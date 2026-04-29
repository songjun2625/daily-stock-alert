"""
SOLAPI 카카오 알림톡 발송 — 시간 윈도우 강제 + 정보성/광고성 분리.

운영 원칙:
  1) 매 발송 직전 lint_message()로 자동 검수.
  2) 발송 시각이 지정 윈도우(한국장 08:30 ±15분 / 미장 22:00 ±15분) 안인지 확인.
     → 윈도우 외 발송 차단 (수동 강제 발송은 명시적 force=True).
  3) 정보성(가입 회원) 메시지만 사용. 광고성 친구톡은 별도 함수 + 별도 21~익일 8시 차단.
  4) 모든 발송은 회원에게 1:多 일방 동일 메시지 (1:1 자문 절대 금지).

SOLAPI Python SDK 사용 가정:  pip install solapi
환경변수: SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER (010-...)
        ALIMTALK_PFID  (카카오 채널 비즈니스 채널 ID)
        ALIMTALK_TEMPLATE_ID_KR / _US / _PAYMENT_D1
"""
from __future__ import annotations
import os, time, logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from screener.config import (
    KST, KR_SEND_HOUR, KR_SEND_MIN, US_SEND_HOUR, US_SEND_MIN,
    AD_BLOCK_START, AD_BLOCK_END,
)
from sender.templates import lint_message, ComplianceError

log = logging.getLogger(__name__)


# ---- 시간 윈도우 강제 ----------------------------------------------------

def _is_within_window(now: datetime, hour: int, minute: int, slack_min: int = 15) -> bool:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return abs((now - target).total_seconds()) <= slack_min * 60


def is_kr_window(now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(KST)).astimezone(KST)
    if now.weekday() >= 5: return False  # 주말 차단
    return _is_within_window(now, KR_SEND_HOUR, KR_SEND_MIN)


def is_us_window(now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(KST)).astimezone(KST)
    if now.weekday() >= 5: return False
    return _is_within_window(now, US_SEND_HOUR, US_SEND_MIN)


def is_ad_blocked(now: Optional[datetime] = None) -> bool:
    """광고성(친구톡) 야간 발송 차단(21~익일 8시 KST)."""
    now = (now or datetime.now(KST)).astimezone(KST)
    h = now.hour
    return h >= AD_BLOCK_START or h < AD_BLOCK_END


# ---- SOLAPI 클라이언트 어댑터 ---------------------------------------------

@dataclass
class AlimtalkResult:
    to: str
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


class SolapiClient:
    """솔라피 어댑터. SDK 미설치 환경에서는 dry-run 모드로 동작."""

    def __init__(self,
                 api_key: Optional[str] = None,
                 api_secret: Optional[str] = None,
                 sender: Optional[str] = None,
                 pfid: Optional[str] = None,
                 dry_run: bool = False):
        self.api_key   = api_key   or os.getenv("SOLAPI_API_KEY")
        self.api_secret= api_secret or os.getenv("SOLAPI_API_SECRET")
        self.sender    = sender    or os.getenv("SOLAPI_SENDER")
        self.pfid      = pfid      or os.getenv("ALIMTALK_PFID")
        self.dry_run   = dry_run or not (self.api_key and self.api_secret and self.pfid)
        self._client = None
        if not self.dry_run:
            try:
                from solapi import SolapiMessageService  # type: ignore
                self._client = SolapiMessageService(self.api_key, self.api_secret)
            except Exception as e:
                log.warning("solapi SDK 로드 실패 → dry-run: %s", e)
                self.dry_run = True

    def send_alimtalk(self, *, to: str, template_id: str, content: str,
                      variables: Optional[dict] = None) -> AlimtalkResult:
        if self.dry_run:
            log.info("[DRY-RUN ALIMTALK] to=%s tmpl=%s\n%s", to, template_id, content)
            return AlimtalkResult(to=to, success=True, message_id="dry-run")
        try:
            from solapi.model import RequestMessage, KakaoOption  # type: ignore
            kakao = KakaoOption(pfId=self.pfid, templateId=template_id,
                                variables=variables or {})
            msg = RequestMessage(to=to, from_=self.sender,
                                 text=content, kakaoOptions=kakao)
            res = self._client.send(msg)
            return AlimtalkResult(to=to, success=True, message_id=str(res))
        except Exception as e:
            log.exception("alimtalk send failed for %s", to)
            return AlimtalkResult(to=to, success=False, error=str(e))


# ---- 일괄 발송 ------------------------------------------------------------

@dataclass
class Subscriber:
    phone: str
    name: str
    plan: str            # lite / standard / pro / annual
    markets: list        # ['kospi', 'us', 'futures']
    is_active: bool = True
    is_alimtalk_optin: bool = True


def _filter_recipients(subs: Iterable[Subscriber], market: str, plan_min: str) -> list[Subscriber]:
    plan_rank = {"lite": 1, "standard": 2, "pro": 3, "annual": 3}
    min_rank = plan_rank.get(plan_min, 1)
    out = []
    for s in subs:
        if not s.is_active or not s.is_alimtalk_optin:
            continue
        if market not in s.markets:
            continue
        if plan_rank.get(s.plan, 0) < min_rank:
            continue
        out.append(s)
    return out


def broadcast_kr(subscribers: Iterable[Subscriber], content: str,
                 client: Optional[SolapiClient] = None,
                 force: bool = False) -> list[AlimtalkResult]:
    if not force and not is_kr_window():
        raise RuntimeError("한국장 발송 윈도우(평일 08:30 ±15분)가 아닙니다. force=True로만 우회 가능.")
    lint_message(content)  # 발송 차단

    client = client or SolapiClient()
    template_id = os.getenv("ALIMTALK_TEMPLATE_ID_KR", "TPL_KR_DAILY")
    targets = _filter_recipients(subscribers, market="kospi", plan_min="lite")
    log.info("KR broadcast: %d recipients", len(targets))

    results = []
    for s in targets:
        results.append(client.send_alimtalk(
            to=s.phone, template_id=template_id, content=content,
            variables={"name": s.name},
        ))
        time.sleep(0.05)  # 단가 8.5원/건 — rate limit 보호
    return results


def broadcast_us(subscribers: Iterable[Subscriber], content: str,
                 client: Optional[SolapiClient] = None,
                 force: bool = False) -> list[AlimtalkResult]:
    if not force and not is_us_window():
        raise RuntimeError("미장 발송 윈도우(평일 22:00 ±15분)가 아닙니다. force=True로만 우회 가능.")
    lint_message(content)

    client = client or SolapiClient()
    template_id = os.getenv("ALIMTALK_TEMPLATE_ID_US", "TPL_US_DAILY")
    targets = _filter_recipients(subscribers, market="us", plan_min="standard")
    log.info("US broadcast: %d recipients", len(targets))

    results = []
    for s in targets:
        results.append(client.send_alimtalk(
            to=s.phone, template_id=template_id, content=content,
            variables={"name": s.name},
        ))
        time.sleep(0.05)
    return results


def send_payment_d1_notice(s: Subscriber, content: str,
                           client: Optional[SolapiClient] = None) -> AlimtalkResult:
    """결제 D-1 안내 — 정보성, 시간 제한 없음."""
    lint_message(content)
    client = client or SolapiClient()
    template_id = os.getenv("ALIMTALK_TEMPLATE_ID_PAYMENT_D1", "TPL_PAY_D1")
    return client.send_alimtalk(
        to=s.phone, template_id=template_id, content=content,
        variables={"name": s.name},
    )
