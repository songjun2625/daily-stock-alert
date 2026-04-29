"""
picks.json 갱신을 외부 웹훅에 알린다.

목적지:
  - Google Apps Script 웹앱 (스프레드시트 기록 + 이메일 발송) — 무료, OAuth 불필요
  - Slack incoming webhook (선택)
  - 일반 HTTP 웹훅 (선택)

환경변수:
  SHEETS_WEBHOOK_URL  — Apps Script 배포 후 발급되는 https://script.google.com/macros/s/.../exec URL
  SLACK_WEBHOOK_URL   — (선택) Slack incoming webhook
  PICKS_JSON          — picks.json 경로 (기본: landing/data/picks.json)
  NOTIFY_MARKETS      — 알림 대상 시장 (기본: 'kr,us,futures', 콤마구분)
  NOTIFY_DRY_RUN      — '1' 이면 실제 전송 없이 콘솔 출력만

사용:
  python tools/notify_update.py
"""
from __future__ import annotations
import os, sys, json, logging
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

log = logging.getLogger("notify")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PICKS_PATH = Path(os.getenv("PICKS_JSON", "landing/data/picks.json"))


def _post_json(url: str, payload: dict, timeout: int = 30) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except URLError as e:
        return 0, str(e)


def main() -> int:
    if not PICKS_PATH.exists():
        log.error("picks.json 없음: %s", PICKS_PATH)
        return 1

    data = json.loads(PICKS_PATH.read_text(encoding="utf-8"))

    markets = [m.strip() for m in os.getenv("NOTIFY_MARKETS", "kr,us,futures").split(",") if m.strip()]
    payload = {
        "updated_at_iso": data.get("updated_at_iso"),
        "updated_at_kst": data.get("updated_at_kst"),
        "fear": data.get("fear", {}),
        "site_url": "https://songjun2625.github.io/daily-stock-alert/today.html",
    }
    for m in markets:
        if m in data:
            payload[m] = data[m]

    dry = os.getenv("NOTIFY_DRY_RUN", "").lower() in ("1", "true", "yes")

    sheets_url = os.getenv("SHEETS_WEBHOOK_URL")
    if sheets_url:
        log.info("→ Sheets webhook (%s markets)", len(markets))
        if dry:
            log.info("[DRY RUN] payload preview: %s", json.dumps({k: payload[k] if k != "fear" else "..." for k in payload})[:500])
        else:
            status, body = _post_json(sheets_url, payload)
            log.info("Sheets webhook: %d %s", status, body[:200])
            if status not in (200, 201, 302):
                log.error("Sheets webhook 실패")
                return 2
    else:
        log.info("SHEETS_WEBHOOK_URL 미설정 — 스킵")

    slack_url = os.getenv("SLACK_WEBHOOK_URL")
    if slack_url and not dry:
        # Slack 은 텍스트 위주 — 짧은 요약만
        lines = [f"*데일리 픽 갱신* — {payload.get('updated_at_kst', '')}"]
        for m, label in [("kr", "🇰🇷 KR"), ("us", "🇺🇸 US"), ("futures", "📊 선물·ETF")]:
            picks = (payload.get(m) or {}).get("picks") or []
            if picks:
                tickers = " · ".join(f"{p.get('ticker')} {p.get('score', 0):.0f}점" for p in picks)
                lines.append(f"{label}: {tickers}")
        lines.append(f"<{payload['site_url']}|오늘의 종목 보기>")
        _post_json(slack_url, {"text": "\n".join(lines)})

    log.info("✅ 알림 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
