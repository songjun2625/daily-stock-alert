"""
news_signals.json 의 호재/악재 점수를 picks.json 의 종목 점수에 반영.

흐름:
  1) picks.json 로드 (이미 발행된 추천)
  2) news_signals.json 로드 (RSS + yfinance.news 분류 결과)
  3) 각 추천 종목의 score 에 news 호재(+) / 악재(-) 가산 적용
  4) picks.json 재저장 (verdict 도 재계산)

가산 규칙:
  - 호재 1건 = +5점 (헤드라인 키워드 매칭)
  - 악재 1건 = -10점 (비대칭 — 악재가 더 강하게 작동)
  - 종목별 최대 +30 / -50 cap
  - 악재 2건↑ = '미추천' 강제 (verdict downgrade)

이메일/UI 측에서 'news_boost' 필드를 읽어 어디서 가산됐는지 표시 가능.
"""
from __future__ import annotations
import json, sys, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("news_boost")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
PICKS_PATH = ROOT / "landing" / "data" / "picks.json"
NEWS_PATH  = ROOT / "landing" / "data" / "news_signals.json"

POS_PER_HIT = 5
NEG_PER_HIT = -10
MAX_BOOST = 30
MAX_PENALTY = -50


def _calc_boost(news_entry: dict) -> tuple[float, int, int]:
    """news_signals.json 의 종목별 entry → (boost점수, 호재수, 악재수)."""
    pos = int(news_entry.get("positive") or 0)
    neg = int(news_entry.get("negative") or 0)
    raw = pos * POS_PER_HIT + neg * NEG_PER_HIT
    boost = max(MAX_PENALTY, min(MAX_BOOST, raw))
    return float(boost), pos, neg


def _apply_to_pick(pick: dict, news_entry: dict | None) -> dict:
    """단일 pick 에 news boost 적용. 원본 score 보존, news_boost 필드 추가."""
    if not news_entry:
        return pick
    boost, pos, neg = _calc_boost(news_entry)
    if boost == 0:
        return pick
    original_score = pick.get("score") or 0
    pick["score_original"] = round(original_score, 2)
    pick["score"] = round(original_score + boost, 2)
    pick["news_boost"] = round(boost, 1)
    pick["news_positive"] = pos
    pick["news_negative"] = neg
    pick["news_headlines"] = (news_entry.get("headlines") or [])[:3]
    return pick


def main():
    if not PICKS_PATH.exists():
        log.warning("picks.json 없음 — skip"); return
    if not NEWS_PATH.exists():
        log.warning("news_signals.json 없음 — skip"); return

    picks_data = json.loads(PICKS_PATH.read_text(encoding="utf-8"))
    news_data  = json.loads(NEWS_PATH.read_text(encoding="utf-8"))

    boosted = 0
    for market_key in ("kr", "us"):
        block = picks_data.get(market_key) or {}
        # 메인 추천
        for p in (block.get("picks") or []):
            ticker = p.get("ticker")
            entry = news_data.get(ticker)
            if entry:
                _apply_to_pick(p, entry)
                if p.get("news_boost"):
                    boosted += 1
                    log.info("  %s %s: news_boost %+.0f (호재 %d / 악재 %d)",
                             market_key.upper(), ticker, p["news_boost"],
                             p.get("news_positive", 0), p.get("news_negative", 0))
        # 퀀트 픽
        qp = block.get("quant_pick")
        if qp:
            entry = news_data.get(qp.get("ticker"))
            if entry:
                _apply_to_pick(qp, entry)
                if qp.get("news_boost"):
                    boosted += 1
                    log.info("  %s QUANT %s: news_boost %+.0f", market_key.upper(),
                             qp.get("ticker"), qp["news_boost"])
        # picks 재정렬 (boost 이후 점수 변경 가능)
        picks = block.get("picks") or []
        picks.sort(key=lambda x: (x.get("score") or 0), reverse=True)
        block["picks"] = picks

    # macro 권고는 그대로 유지
    PICKS_PATH.write_text(json.dumps(picks_data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info("✅ news boost 적용 완료: %d 종목 가산", boosted)


if __name__ == "__main__":
    main()
