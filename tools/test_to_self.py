"""
본인 폰으로 테스트 발송 — 검수 통과 전후 모두 사용 가능.

사용법:
    # (1) 솔라피 SMS — 검수 불필요, 즉시 본인 폰 도착 (가독성 점검용)
    python tools/test_to_self.py sms 010-1234-5678 KR_DAILY

    # (2) 친구톡 — 채널 친구 추가 후 가능, 광고성이지만 디자인 점검용
    python tools/test_to_self.py friendtalk 010-1234-5678 US_DAILY

    # (3) 알림톡 — 카카오 검수 통과 후만 가능 (정식 운영 채널)
    python tools/test_to_self.py alimtalk 010-1234-5678 PAYMENT_D1

지원 템플릿: KR_DAILY / US_DAILY / PAYMENT_D1 / WELCOME / TRIAL_END

환경변수 (.env):
    SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER, ALIMTALK_PFID
    ALIMTALK_TEMPLATE_ID_KR / _US / _PAYMENT_D1 / _WELCOME / _TRIAL_END (검수 통과 후 입력)
"""
from __future__ import annotations
import os, sys, json, logging
from pathlib import Path

# Load .env if exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "kakao-templates" / "templates.json"

log = logging.getLogger("test_to_self")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# ---- 템플릿 로드 + 변수 예시값 자동 치환 -----------------------------------

def render_sample(code: str) -> tuple[str, dict]:
    raw = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    template = next((t for t in raw["templates"] if t["code"].startswith(code) or t["name"].startswith(code)), None)
    if template is None:
        # 단축 코드 매핑
        alias = {
            "KR_DAILY":   "KR_DAILY_V1",
            "US_DAILY":   "US_DAILY_V1",
            "PAYMENT_D1": "PAYMENT_D1_V1",
            "WELCOME":    "WELCOME_V1",
            "TRIAL_END":  "TRIAL_END_V1",
        }
        full_code = alias.get(code.upper(), code)
        template = next((t for t in raw["templates"] if t["code"] == full_code), None)
    if template is None:
        raise SystemExit(f"템플릿을 찾을 수 없습니다: {code}\n사용 가능: KR_DAILY / US_DAILY / PAYMENT_D1 / WELCOME / TRIAL_END")

    body = template["body"]
    variables = {v["key"]: v["example"] for v in template.get("variables", [])}
    for k, v in variables.items():
        body = body.replace(f"#{{{k}}}", v)
    return body, template


# ---- 솔라피 클라이언트 ---------------------------------------------------

def _client():
    api_key = os.getenv("SOLAPI_API_KEY")
    api_secret = os.getenv("SOLAPI_API_SECRET")
    if not (api_key and api_secret):
        raise SystemExit("환경변수 SOLAPI_API_KEY / SOLAPI_API_SECRET 가 비어 있습니다. .env 파일을 확인해주세요.")
    try:
        from solapi import SolapiMessageService  # type: ignore
    except ImportError:
        raise SystemExit("solapi SDK 미설치. 'pip install solapi' 후 다시 시도하세요.")
    return SolapiMessageService(api_key, api_secret)


def _normalize_phone(p: str) -> str:
    return "".join(ch for ch in p if ch.isdigit())


# ---- 모드별 발송 ---------------------------------------------------------

def send_sms(to: str, body: str) -> None:
    """검수 불필요. 본문이 길면 자동으로 LMS(2,000자)로 전환."""
    from solapi.model import RequestMessage  # type: ignore
    sender = os.getenv("SOLAPI_SENDER")
    if not sender:
        raise SystemExit("SOLAPI_SENDER 미설정 — 사전 발신번호 등록(24h)이 필요합니다.")
    msg = RequestMessage(to=_normalize_phone(to), from_=sender, text=body)
    res = _client().send(msg)
    log.info("SMS/LMS 발송 완료: %s", res)


def send_friendtalk(to: str, body: str) -> None:
    """카카오 채널 친구만 받을 수 있음. 광고성 — 21~익일 8시 차단."""
    from solapi.model import RequestMessage, KakaoOption  # type: ignore
    sender = os.getenv("SOLAPI_SENDER")
    pfid = os.getenv("ALIMTALK_PFID")
    if not pfid:
        raise SystemExit("ALIMTALK_PFID 미설정 — 솔라피에서 카카오 채널 연동이 필요합니다.")
    kakao = KakaoOption(pfId=pfid, disableSms=False)  # 친구톡 모드 (templateId 없음)
    msg = RequestMessage(to=_normalize_phone(to), from_=sender,
                         text=body, kakaoOptions=kakao, type="CTA")
    res = _client().send(msg)
    log.info("친구톡 발송 완료: %s", res)


def send_alimtalk(to: str, code: str, template: dict) -> None:
    """검수 통과 후 발급된 템플릿 ID로 발송. 변수는 template_variables에 자동 매핑."""
    from solapi.model import RequestMessage, KakaoOption  # type: ignore
    sender = os.getenv("SOLAPI_SENDER")
    pfid = os.getenv("ALIMTALK_PFID")
    env_map = {
        "KR_DAILY_V1":   "ALIMTALK_TEMPLATE_ID_KR",
        "US_DAILY_V1":   "ALIMTALK_TEMPLATE_ID_US",
        "PAYMENT_D1_V1": "ALIMTALK_TEMPLATE_ID_PAYMENT_D1",
        "WELCOME_V1":    "ALIMTALK_TEMPLATE_ID_WELCOME",
        "TRIAL_END_V1":  "ALIMTALK_TEMPLATE_ID_TRIAL_END",
    }
    template_id = os.getenv(env_map[template["code"]])
    if not template_id:
        raise SystemExit(
            f"{env_map[template['code']]} 미설정 — 카카오 검수 통과 후 발급된 템플릿 ID를 .env 에 입력해야 합니다.\n"
            f"검수 신청은 솔라피 → [알림톡] → [템플릿 관리]에서 templates.json 의 {template['code']} 양식 그대로 등록."
        )
    variables = {f"#{{{v['key']}}}": v["example"] for v in template.get("variables", [])}
    body = template["body"]
    for k, v in variables.items():
        body = body.replace(k, v)
    kakao = KakaoOption(pfId=pfid, templateId=template_id, variables=variables, disableSms=False)
    msg = RequestMessage(to=_normalize_phone(to), from_=sender,
                         text=body, kakaoOptions=kakao)
    res = _client().send(msg)
    log.info("알림톡 발송 완료: %s", res)


# ---- main ----------------------------------------------------------------

USAGE = """\
사용법:
  python tools/test_to_self.py <mode> <phone> <template_code>

모드:
  sms         — 즉시 발송 (검수 불필요, 가독성 점검)
  friendtalk  — 카카오 친구 추가 후 가능 (광고성, 디자인 점검)
  alimtalk    — 카카오 검수 통과 후만 가능 (정식)
  preview     — 발송 안 하고 본문만 콘솔에 출력

템플릿: KR_DAILY / US_DAILY / PAYMENT_D1 / WELCOME / TRIAL_END
"""


def main():
    if len(sys.argv) < 4 and not (len(sys.argv) >= 3 and sys.argv[1] == "preview"):
        print(USAGE); sys.exit(1)

    mode = sys.argv[1]
    if mode == "preview":
        code = sys.argv[2]
        body, _ = render_sample(code)
        print("─" * 60)
        print(body)
        print("─" * 60)
        print(f"본문 길이: {len(body)}자 (알림톡 1,000자 / SMS 90바이트 / LMS 2,000자)")
        return

    phone = sys.argv[2] or os.getenv("TEST_RECIPIENT") or os.getenv("SOLAPI_SENDER")
    code = sys.argv[3]
    body, template = render_sample(code)

    print(f"\n[{mode.upper()}] → {phone}")
    print("─" * 60); print(body); print("─" * 60)

    confirm = input("\n실제 발송하시겠습니까? (y/N): ").strip().lower()
    if confirm != "y":
        print("취소되었습니다."); return

    if mode == "sms":
        send_sms(phone, body)
    elif mode == "friendtalk":
        send_friendtalk(phone, body)
    elif mode == "alimtalk":
        send_alimtalk(phone, code, template)
    else:
        print(USAGE); sys.exit(1)


if __name__ == "__main__":
    main()
