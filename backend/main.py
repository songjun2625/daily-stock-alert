"""
FastAPI 회원가입 + 결제 리다이렉트 백엔드.

스택:
  - FastAPI (Railway/Render 배포)
  - Supabase (Postgres + Auth — 운영) / 로컬 JSON (개발)
  - 토스페이먼츠 정기결제 (`billingKey` 발급 → 7일 무료 후 자동결제)

엔드포인트:
  POST /api/subscribe           — 회원가입 → Toss billing 페이지 redirect_url 발급
  POST /api/payments/confirm    — Toss 결제 승인 콜백 (billingKey 저장)
  POST /api/cancel              — 해지 (즉시 발송 중단)
  GET  /api/me                  — 마이페이지 조회
  POST /webhooks/alimtalk       — 발송 결과 콜백 (실패시 SMS 폴백)

면책: 본 서비스는 1:多 일방 발송 정보성 메시지만 운영. 회원별 1:1 자문은 절대 제공하지 않음.
"""
from __future__ import annotations
import os, json, secrets, hashlib, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator

log = logging.getLogger(__name__)

app = FastAPI(title="Daily Pick API", version="0.1.0")

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1,https://dailypick.kr").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---- Models -------------------------------------------------------------

PLAN_PRICES = {"lite": 9_900, "standard": 19_900, "pro": 39_900, "annual": 390_000}
PLAN_LABELS = {"lite": "Lite", "standard": "Standard", "pro": "Pro", "annual": "Annual"}


class SubscribeIn(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    phone: str
    email: EmailStr
    markets: list[Literal["kospi", "us", "futures"]] = Field(min_length=1)
    plan: Literal["lite", "standard", "pro", "annual"]
    agree_terms: bool
    agree_privacy: bool
    agree_alimtalk: bool
    agree_marketing: bool = False

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if not (10 <= len(digits) <= 11):
            raise ValueError("휴대전화 번호 형식이 올바르지 않습니다.")
        return digits

    @field_validator("agree_terms", "agree_privacy", "agree_alimtalk")
    @classmethod
    def _required_consents(cls, v: bool) -> bool:
        if not v:
            raise ValueError("필수 동의 항목입니다.")
        return v


class SubscribeOut(BaseModel):
    member_id: str
    redirect_url: str
    plan: str
    amount: int
    trial_days: int = 7


# ---- Storage adapter ----------------------------------------------------

class Store:
    """Supabase 또는 로컬 JSON. SUPABASE_URL 환경변수가 있으면 Supabase 사용."""

    def __init__(self):
        self.use_supabase = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"))
        self._client = None
        self._local = Path(os.getenv("LOCAL_DB", "subscribers.json"))
        if self.use_supabase:
            try:
                from supabase import create_client  # type: ignore
                self._client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
            except Exception as e:
                log.warning("supabase init failed → JSON: %s", e)
                self.use_supabase = False

    def insert_member(self, payload: dict) -> str:
        member_id = secrets.token_urlsafe(12)
        record = {**payload, "member_id": member_id,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "is_active": True, "billing_key": None,
                  "trial_ends_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}
        if self.use_supabase:
            self._client.table("members").insert(record).execute()
        else:
            data = []
            if self._local.exists():
                data = json.loads(self._local.read_text(encoding="utf-8"))
            data.append(record)
            self._local.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return member_id

    def get_member(self, member_id: str) -> dict | None:
        if self.use_supabase:
            res = self._client.table("members").select("*").eq("member_id", member_id).execute()
            return (res.data or [None])[0]
        if not self._local.exists(): return None
        for r in json.loads(self._local.read_text(encoding="utf-8")):
            if r.get("member_id") == member_id:
                return r
        return None

    def deactivate(self, member_id: str) -> bool:
        if self.use_supabase:
            self._client.table("members").update({"is_active": False}).eq("member_id", member_id).execute()
            return True
        if not self._local.exists(): return False
        data = json.loads(self._local.read_text(encoding="utf-8"))
        ok = False
        for r in data:
            if r.get("member_id") == member_id:
                r["is_active"] = False; ok = True
        self._local.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ok


store = Store()


# ---- Endpoints ----------------------------------------------------------

@app.post("/api/subscribe", response_model=SubscribeOut)
def subscribe(payload: SubscribeIn):
    amount = PLAN_PRICES[payload.plan]
    member_id = store.insert_member(payload.model_dump())

    # 토스페이먼츠 빌링키 발급 페이지로 리다이렉트
    # 실제 구현: Toss 서버 → /api/payments/confirm 콜백에서 billingKey 저장
    base = os.getenv("PAYMENT_REDIRECT_BASE", "https://api.tosspayments.com/v1/billing/authorizations")
    redirect_url = (
        f"{base}?customerKey={member_id}"
        f"&customerEmail={payload.email}"
        f"&customerName={payload.name}"
        f"&successUrl={os.getenv('PAYMENT_SUCCESS_URL', 'https://dailypick.kr/payments/success')}"
        f"&failUrl={os.getenv('PAYMENT_FAIL_URL', 'https://dailypick.kr/payments/fail')}"
    )
    return SubscribeOut(
        member_id=member_id, redirect_url=redirect_url,
        plan=PLAN_LABELS[payload.plan], amount=amount,
    )


class CancelIn(BaseModel):
    member_id: str


@app.post("/api/cancel")
def cancel(payload: CancelIn):
    ok = store.deactivate(payload.member_id)
    if not ok:
        raise HTTPException(404, "회원을 찾을 수 없습니다.")
    return {"ok": True, "message": "발송이 즉시 중단되었습니다. 환불 정책은 약관을 따릅니다."}


@app.get("/api/me")
def me(member_id: str):
    m = store.get_member(member_id)
    if not m:
        raise HTTPException(404)
    return {"member_id": m["member_id"], "name": m["name"], "plan": m["plan"],
            "markets": m["markets"], "is_active": m["is_active"],
            "trial_ends_at": m["trial_ends_at"]}


@app.post("/api/payments/confirm")
async def payments_confirm(req: Request):
    """토스 결제 승인 콜백. authKey + customerKey 로 billingKey 발급."""
    body = await req.json()
    log.info("toss confirm callback: %s", body)
    # 실제 구현: requests.post(toss_billing_url, ...) → billingKey 저장
    # 여기서는 스켈레톤만.
    return {"ok": True}


@app.post("/webhooks/alimtalk")
async def alimtalk_webhook(req: Request):
    """SOLAPI 발송 결과 콜백. 실패 시 SMS 폴백 큐에 적재."""
    body = await req.json()
    log.info("alimtalk webhook: %s", body)
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": "0.1.0"}
