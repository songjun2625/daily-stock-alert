"""
스크리너 결과를 랜딩 페이지가 읽는 JSON으로 발행.

GitHub Actions가 매일 두 번 호출:
  - KR: 평일 06:30 UTC (KST 15:30, 한국장 종료 30분 후) → kr 섹션 갱신
  - US: 평일 13:00 UTC (KST 22:00, 미장 시작 30분 전) → us 섹션 갱신
  - Actions가 변경된 picks.json 만 main 에 자동 커밋 → Pages가 재배포

JSON 구조: 기존 데이터를 보존한 채 해당 시장 섹션만 부분 갱신 (다른 시장의 직전 결과 유지).
"""
from __future__ import annotations
import os, json, logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import KST, TOP_N_KR, TOP_N_US
from .screener_us import screen_us, market_traffic_light
from .screener_kr import screen_kr

log = logging.getLogger(__name__)
PICKS_PATH = Path(os.getenv("PICKS_JSON", "landing/data/picks.json"))


def _now_kst() -> datetime:
    return datetime.now(KST)


def _format_kst(dt: datetime) -> str:
    weekday = "월화수목금토일"[dt.weekday()]
    return f"{dt.strftime('%Y.%m.%d')} ({weekday}) {dt.strftime('%H:%M')} KST"


def _load_existing() -> dict:
    if PICKS_PATH.exists():
        try:
            return json.loads(PICKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write(data: dict) -> None:
    PICKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PICKS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info("picks.json written: %s", PICKS_PATH)


def publish_kr() -> dict:
    log.info("publish KR start")
    picks = screen_kr(top_n=TOP_N_KR)
    now = _now_kst()
    payload = _load_existing()
    payload["kr"] = {
        "picks": [asdict(p) for p in picks],
        "updated_at_iso": now.isoformat(),
        "updated_at_kst": _format_kst(now),
    }
    payload["updated_at_iso"] = now.isoformat()
    payload["updated_at_kst"] = _format_kst(now)
    _write(payload)
    return {"market": "kr", "n": len(picks), "tickers": [p.ticker for p in picks]}


def publish_us() -> dict:
    log.info("publish US start")
    picks = screen_us(top_n=TOP_N_US)
    light = market_traffic_light()
    now = _now_kst()
    payload = _load_existing()
    payload["us"] = {
        "picks": [asdict(p) for p in picks],
        "traffic_light": light,
        "updated_at_iso": now.isoformat(),
        "updated_at_kst": _format_kst(now),
    }
    payload["updated_at_iso"] = now.isoformat()
    payload["updated_at_kst"] = _format_kst(now)
    _write(payload)
    return {"market": "us", "n": len(picks), "tickers": [p.ticker for p in picks],
            "vix": light["vix"]}


def publish_both() -> dict:
    return {"kr": publish_kr(), "us": publish_us()}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "both"
    if cmd == "kr": print(publish_kr())
    elif cmd == "us": print(publish_us())
    else: print(publish_both())
