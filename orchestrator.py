"""
일일 자동 실행 오케스트레이터.

GitHub Actions 또는 서버 cron 으로 다음 두 잡을 매일 평일에 실행:
  1) 08:00 KST  → run_kr()  : KR 스크리너 → 사람 검수 큐 → 08:30 발송
  2) 21:30 KST  → run_us()  : US 스크리너 → 사람 검수 큐 → 22:00 발송

`AUTO_SEND=true` 환경변수가 있으면 검수 단계를 건너뛰고 즉시 발송 (파일럿용 비추천).
실서비스에서는 슬랙 웹훅으로 검수 알림 → 운영자가 한 번 확인 후 confirm.py 수동 실행.
"""
from __future__ import annotations
import os, sys, json, logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from screener.config import KST, TOP_N_KR, TOP_N_US
from screener.screener_us import screen_us, market_traffic_light
from screener.screener_kr import screen_kr
from sender.templates import (
    build_kakao_message_kr, build_kakao_message_us, lint_message,
)
from sender.send_alimtalk import (
    broadcast_kr, broadcast_us, SolapiClient, Subscriber, is_kr_window, is_us_window,
)

log = logging.getLogger(__name__)
QUEUE_DIR = Path(os.getenv("QUEUE_DIR", "queue"))
QUEUE_DIR.mkdir(exist_ok=True, parents=True)


def _load_subscribers() -> list[Subscriber]:
    """운영 시 Supabase에서 조회. 여기서는 로컬 JSON 폴백."""
    src = os.getenv("SUBSCRIBERS_JSON", "subscribers.json")
    p = Path(src)
    if not p.exists():
        log.warning("subscribers file not found: %s", src)
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [Subscriber(**r) for r in raw]


def _save_queue(name: str, payload: dict) -> Path:
    out = QUEUE_DIR / f"{datetime.now(KST).strftime('%Y%m%d_%H%M')}_{name}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    return out


def run_kr(auto_send: bool = False) -> dict:
    log.info("KR screener start")
    picks = screen_kr(top_n=TOP_N_KR)
    msg = build_kakao_message_kr(picks, datetime.now(KST))
    lint_message(msg)
    payload = {"market": "kr", "picks": [asdict(p) for p in picks], "message": msg}
    qpath = _save_queue("kr", payload)
    log.info("KR queued at %s — picks: %s", qpath, [p.ticker for p in picks])

    if auto_send:
        if not is_kr_window():
            log.warning("KR window 외 — 자동 발송 차단")
            return {"queued": str(qpath), "sent": 0, "blocked": "window"}
        results = broadcast_kr(_load_subscribers(), msg, client=SolapiClient())
        return {"queued": str(qpath), "sent": sum(1 for r in results if r.success),
                "failed": sum(1 for r in results if not r.success)}
    return {"queued": str(qpath), "sent": 0, "next_step": "운영자 검수 후 confirm 실행"}


def run_us(auto_send: bool = False) -> dict:
    log.info("US screener start")
    picks = screen_us(top_n=TOP_N_US)
    light = market_traffic_light()
    msg = build_kakao_message_us(picks, datetime.now(KST))
    lint_message(msg)
    payload = {
        "market": "us",
        "traffic_light": light,
        "picks": [asdict(p) for p in picks],
        "message": msg,
    }
    qpath = _save_queue("us", payload)
    log.info("US queued at %s — picks: %s", qpath, [p.ticker for p in picks])

    if auto_send:
        if not is_us_window():
            log.warning("US window 외 — 자동 발송 차단")
            return {"queued": str(qpath), "sent": 0, "blocked": "window"}
        results = broadcast_us(_load_subscribers(), msg, client=SolapiClient())
        return {"queued": str(qpath), "sent": sum(1 for r in results if r.success),
                "failed": sum(1 for r in results if not r.success)}
    return {"queued": str(qpath), "sent": 0, "next_step": "운영자 검수 후 confirm 실행"}


def confirm_send(queue_file: str) -> dict:
    """검수 완료된 큐 파일을 실제 발송."""
    p = Path(queue_file)
    payload = json.loads(p.read_text(encoding="utf-8"))
    msg = payload["message"]
    market = payload["market"]
    subs = _load_subscribers()
    if market == "kr":
        results = broadcast_kr(subs, msg)
    else:
        results = broadcast_us(subs, msg)
    return {"sent": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run_kr"
    auto = os.getenv("AUTO_SEND", "").lower() in ("1", "true", "yes")
    if cmd == "run_kr":
        print(run_kr(auto_send=auto))
    elif cmd == "run_us":
        print(run_us(auto_send=auto))
    elif cmd == "confirm":
        print(confirm_send(sys.argv[2]))
    else:
        print("usage: orchestrator.py [run_kr|run_us|confirm <queue_file>]")
