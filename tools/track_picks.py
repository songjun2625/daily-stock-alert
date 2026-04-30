"""
일일 추천 종목을 라이브 포지션으로 누적·청산하는 트래커.

매일 orchestrator 가 picks.json 을 갱신한 직후 이 스크립트가 실행됨:
  1) picks.json 의 KR/US 추천 종목을 읽어, 새로 진입한 포지션은 open_positions 에 추가
  2) 기존 open_positions 각 종목에 대해 현재가를 조회
     → 손절가 도달 / 목표가 도달 / hold_days 초과 시 청산 → closed_trades 로 이동
     → 진행 중이면 current_price·pnl_pct·days_held 갱신
  3) 누적 통계(summary, by_market) 재계산
  4) landing/data/live_trades.json 에 저장 (UI 가 실시간 표시)

규칙 (백테스트와 동일):
  - 손절: 종가가 stoploss 이하 → 그 가격으로 청산
  - 목표: 종가가 target 이상 → 그 가격으로 청산
  - 만기: hold_days(5거래일) 초과 → 종가 청산
  - 거래비용: 왕복 0.25% (수수료 0.05% + 슬리피지 0.2%) — 백테스트와 동일

출력:
  landing/data/live_trades.json
    {
      "open_positions": [ {ticker, market, name, entry_date, entry_price,
                           stoploss, target, hold_days, current_price, pnl_pct,
                           days_held, status:"open"} ],
      "closed_trades":  [ {ticker, market, entry_date, exit_date, bars_held,
                           pnl_pct, reason, name} ],
      "summary": { total_trades, win_rate_pct, cum_return_pct, avg_pnl_pct,
                   avg_hold_days, best_pct, worst_pct },
      "by_market": { "kr": {...}, "us": {...} },
      "period_start": "...", "period_end": "...",
      "updated_at_kst": "..."
    }
"""
from __future__ import annotations
import json, logging, sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import data_sources as ds
from screener.config import KST

log = logging.getLogger("track_picks")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "landing" / "data"
PICKS_PATH = DATA_DIR / "picks.json"
LIVE_PATH  = DATA_DIR / "live_trades.json"

HOLD_DAYS = 5
COMM_ROUND_TRIP = 0.0025  # 0.25% 왕복 거래비용


def _now_kst() -> datetime:
    return datetime.now(KST)


def _today_str() -> str:
    return _now_kst().strftime("%Y-%m-%d")


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("failed to read %s: %s", path, e)
        return default


def _bars_between(entry_date: str, today: str) -> int:
    """주말 제외 거래일 수 — 단순 근사 (5/7로 환산). 정밀 계산은 시장 캘린더 필요."""
    try:
        d0 = datetime.strptime(entry_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return 0
    days = (d1 - d0).days
    if days <= 0: return 0
    # 주말 빼기 — 간단 근사
    full_weeks, rem = divmod(days, 7)
    return max(0, full_weeks * 5 + min(rem, 5))


def _fetch_current_price(ticker: str, market: str) -> float | None:
    """가장 최신 종가/실시간가. 캐싱은 fetch_history 가 처리."""
    try:
        df = ds.fetch_history(ticker, market=market, period_days=10)
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception as e:
        log.warning("price fetch failed for %s (%s): %s", ticker, market, e)
        return None


def _open_position_from_pick(pick: dict, market: str, today: str) -> dict:
    """picks.json 의 한 항목 → open_position dict."""
    entry_price = float(pick.get("price") or pick.get("entry_low") or 0)
    return {
        "ticker": pick["ticker"],
        "market": market,
        "name": pick.get("name", ""),
        "sector": pick.get("sector", ""),
        "entry_date": today,
        "entry_price": entry_price,
        "stoploss": float(pick.get("stoploss") or 0),
        "target": float(pick.get("target") or 0),
        "hold_days": int(pick.get("narrative", {}).get("hold_days", HOLD_DAYS) or HOLD_DAYS),
        "current_price": entry_price,
        "pnl_pct": 0.0,
        "days_held": 0,
        "status": "open",
        "score": float(pick.get("score") or 0),
    }


def _update_position(pos: dict, today: str) -> tuple[dict, dict | None]:
    """포지션 한 건 — 현재가 조회 후 청산 여부 판단.

    반환: (갱신된 open_position 또는 None, 청산되면 closed_trade dict 또는 None)
    """
    market = pos.get("market", "us")
    cur = _fetch_current_price(pos["ticker"], market)
    if cur is None:
        # 가격 조회 실패 — 기존 정보 유지
        return pos, None

    entry = pos["entry_price"]
    target = pos["target"]
    stop = pos["stoploss"]
    hold_days = pos.get("hold_days", HOLD_DAYS)
    days_held = _bars_between(pos["entry_date"], today)

    # 청산 판단 — 백테스트와 동일 규칙
    exit_price = None
    reason = None
    if stop > 0 and cur <= stop:
        exit_price = stop          # 손절가 도달 — 보수적으로 손절가에 청산
        reason = "stop"
    elif target > 0 and cur >= target:
        exit_price = target        # 목표가 도달 — 목표가에 청산
        reason = "target"
    elif days_held >= hold_days:
        exit_price = cur           # 만기 — 종가 청산
        reason = "time"

    if exit_price is not None:
        gross = (exit_price - entry) / entry if entry > 0 else 0.0
        pnl = (gross - COMM_ROUND_TRIP) * 100
        closed = {
            "ticker": pos["ticker"],
            "market": market,
            "name": pos.get("name", ""),
            "entry_date": pos["entry_date"],
            "exit_date": today,
            "bars_held": days_held,
            "pnl_pct": round(pnl, 3),
            "reason": reason,
        }
        return None, closed

    # 진행 중 — 현재가·P&L 갱신
    pnl = ((cur - entry) / entry * 100) if entry > 0 else 0.0
    pos.update({
        "current_price": round(cur, 4),
        "pnl_pct": round(pnl, 3),
        "days_held": days_held,
    })
    return pos, None


def _summary(closed: list[dict]) -> dict:
    if not closed:
        return {"total_trades": 0, "win_rate_pct": 0.0, "cum_return_pct": 0.0,
                "avg_pnl_pct": 0.0, "avg_hold_days": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
    pnls = [t["pnl_pct"] for t in closed]
    holds = [t.get("bars_held", 0) for t in closed]
    wins = [p for p in pnls if p > 0]
    cum = 1.0
    for p in pnls:
        cum *= (1 + p / 100)
    return {
        "total_trades": len(closed),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "cum_return_pct": round((cum - 1) * 100, 2),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
    }


def _by_market(closed: list[dict]) -> dict:
    out = {}
    for m in ("kr", "us"):
        sub = [t for t in closed if t.get("market") == m]
        out[m] = _summary(sub)
    return out


def update_live_trades(picks_path: Path = PICKS_PATH,
                       live_path: Path = LIVE_PATH) -> dict:
    today = _today_str()
    picks_data = _load_json(picks_path, {})
    live_data  = _load_json(live_path, {
        "open_positions": [],
        "closed_trades": [],
        "summary": {},
        "by_market": {},
        "period_start": today,
        "period_end": today,
        "updated_at_iso": "",
        "updated_at_kst": "",
    })

    open_positions: list[dict] = list(live_data.get("open_positions", []))
    closed_trades: list[dict]  = list(live_data.get("closed_trades", []))

    # 1) 신규 추천 종목 → 같은 종목이 이미 open 이면 추가하지 않음 (중복 방지)
    open_keys = {(p["ticker"], p["market"]) for p in open_positions}
    for market_key in ("kr", "us"):
        market_block = picks_data.get(market_key, {}) or {}
        for pick in (market_block.get("picks") or []):
            if not pick.get("ticker"):
                continue
            key = (pick["ticker"], market_key)
            if key in open_keys:
                continue
            new_pos = _open_position_from_pick(pick, market_key, today)
            open_positions.append(new_pos)
            open_keys.add(key)
            log.info("new open position: %s (%s) entry=%.2f", pick["ticker"], market_key, new_pos["entry_price"])

    # 2) 모든 open 포지션 갱신 (현재가 조회 + 청산 판단)
    survivors: list[dict] = []
    for pos in open_positions:
        updated, closed = _update_position(pos, today)
        if closed is not None:
            closed_trades.append(closed)
            log.info("closed: %s reason=%s pnl=%.2f%%", closed["ticker"], closed["reason"], closed["pnl_pct"])
        elif updated is not None:
            survivors.append(updated)

    # 3) 정렬 — open: 진입일 최신순, closed: 청산일 최신순
    survivors.sort(key=lambda p: p["entry_date"], reverse=True)
    closed_trades.sort(key=lambda t: t["exit_date"], reverse=True)

    # 4) 통계
    summary = _summary(closed_trades)
    by_market = _by_market(closed_trades)

    period_start = live_data.get("period_start") or today
    if closed_trades:
        period_start = min(period_start, min(t["entry_date"] for t in closed_trades))
    if survivors:
        period_start = min(period_start, min(p["entry_date"] for p in survivors))

    out = {
        "open_positions": survivors,
        "closed_trades": closed_trades,
        "summary": summary,
        "by_market": by_market,
        "period_start": period_start,
        "period_end": today,
        "updated_at_iso": _now_kst().isoformat(timespec="seconds"),
        "updated_at_kst": _now_kst().strftime("%Y.%m.%d (%a) %H:%M KST"),
    }
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("live_trades.json updated: %d open, %d closed", len(survivors), len(closed_trades))
    return out


if __name__ == "__main__":
    update_live_trades()
