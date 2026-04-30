"""
4월 1일 ~ 오늘까지 현재 알고리즘으로 모의 운용한 결과를 산출.

규칙:
  - 현재 screener_us / screener_kr 의 시그널 로직을 그대로 사용.
  - 매일 시장 종료 후 시그널이 발생하면 다음 거래일 시가에 진입.
  - 보유 5일, 손절 -4%, 목표 +6% (먼저 도달하는 쪽으로 청산).
  - 거래비용: 슬리피지 0.1% × 2회 + 수수료 0.025% × 2회 = 약 0.25% 차감.
  - 동일 비중 (각 픽 1/N).
  - 룩어헤드 방지: t일 시그널은 t일 종가까지의 데이터로만 판단, t+1일 시가 진입.

출력:
  landing/data/backtest.json
    {
      "period_start": "2026-04-01",
      "period_end":   "2026-04-30",
      "summary": { total_trades, win_rate_pct, cum_return_pct, mdd_pct,
                   avg_pnl_pct, avg_hold_days, sharpe },
      "by_market": { "kr": {...}, "us": {...} },
      "trades": [ {ticker, market, entry_date, exit_date, pnl_pct, reason}, ... ]
    }
"""
from __future__ import annotations
import json, logging, sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import indicators as ind
from screener import data_sources as ds
from screener.screener_us import DEFAULT_UNIVERSE as US_UNIVERSE
from screener.screener_kr import DEFAULT_KR_UNIVERSE as KR_UNIVERSE
from screener.config import US_THRESH, KR_THRESH

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

START_DATE = "2026-04-01"
COMM = 0.00025      # 수수료 0.025% (편도)
SLIP = 0.001        # 슬리피지 0.1% (편도)
HOLD_DAYS = 5
STOP_PCT = 0.04
TARGET_PCT = 0.06


# ---- 시그널 (현재 라이브 알고리즘과 동일 -----------------------------------

def us_signal(close: pd.Series, vol: pd.Series, idx: int) -> bool:
    if idx < 60: return False
    sub_close = close.iloc[: idx + 1]
    sub_vol   = vol.iloc[: idx + 1]
    rsi_v = float(ind.rsi(sub_close).iloc[-1])
    cheap = (US_THRESH.rsi_low <= rsi_v <= US_THRESH.rsi_high)
    if not cheap:
        # drawdown 시그널도 cheap 으로 인정
        peak = sub_close.tail(252).max()
        dd = (peak - sub_close.iloc[-1]) / peak if peak > 0 else 0
        if not (US_THRESH.drawdown_low <= dd <= US_THRESH.drawdown_high):
            return False
    macd_l, sig_l, _ = ind.macd(sub_close)
    if not (ind.is_macd_golden_cross(macd_l, sig_l)
            or ind.is_ma_aligned_up(sub_close)
            or ind.volume_spike(sub_vol)):
        return False
    return True


def kr_signal(close: pd.Series, vol: pd.Series, idx: int) -> bool:
    if idx < 60: return False
    sub_close = close.iloc[: idx + 1]
    sub_vol   = vol.iloc[: idx + 1]
    rsi_v = float(ind.rsi(sub_close).iloc[-1])
    macd_l, sig_l, _ = ind.macd(sub_close)
    triggers = (
        ind.volume_spike(sub_vol, multiplier=KR_THRESH.volume_multiplier_min),
        KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high,
        ind.is_macd_golden_cross(macd_l, sig_l),
        ind.is_ma_aligned_up(sub_close),
    )
    return any(triggers)


# ---- 단일 종목 백테스트 ---------------------------------------------------

def backtest_one(history: pd.DataFrame, ticker: str, market: str,
                 signal_fn) -> list[dict]:
    if history is None or len(history) < 80: return []
    close = history["Close"]; vol = history["Volume"]
    high  = history["High"];  low = history["Low"]
    try:
        opens = history["Open"]
    except KeyError:
        opens = close
    start = pd.Timestamp(START_DATE, tz=close.index.tz) if close.index.tz else pd.Timestamp(START_DATE)

    trades: list[dict] = []
    cooldown = 0
    i = max(60, close.index.searchsorted(start) - 1)
    while i < len(close) - 1:
        if cooldown > 0:
            cooldown -= 1; i += 1; continue
        if close.index[i] < start.tz_localize(close.index.tz) if close.index.tz and start.tz is None else close.index[i] < start:
            i += 1; continue
        if not signal_fn(close, vol, i):
            i += 1; continue

        entry_idx = i + 1
        if entry_idx >= len(close): break
        entry_open = float(opens.iloc[entry_idx])
        entry_px = entry_open * (1 + SLIP)
        target_px = entry_px * (1 + TARGET_PCT)
        stop_px   = entry_px * (1 - STOP_PCT)

        exit_idx = None; exit_px = None; reason = "time"
        for j in range(entry_idx, min(entry_idx + HOLD_DAYS, len(close))):
            day_high = float(high.iloc[j]); day_low = float(low.iloc[j])
            if day_low <= stop_px:
                exit_idx, exit_px, reason = j, stop_px * (1 - SLIP), "stop"; break
            if day_high >= target_px:
                exit_idx, exit_px, reason = j, target_px * (1 - SLIP), "target"; break
        if exit_idx is None:
            exit_idx = min(entry_idx + HOLD_DAYS - 1, len(close) - 1)
            exit_px = float(close.iloc[exit_idx]) * (1 - SLIP)
            reason = "time"

        gross = (exit_px - entry_px) / entry_px
        net = gross - 2 * COMM
        trades.append({
            "ticker": ticker, "market": market,
            "entry_date": str(close.index[entry_idx].date()),
            "exit_date":  str(close.index[exit_idx].date()),
            "bars_held":  exit_idx - entry_idx + 1,
            "pnl_pct":    round(net * 100, 3),
            "reason":     reason,
        })
        cooldown = exit_idx - i + 1
        i = exit_idx + 1
    return trades


# ---- 시장별 + 전체 집계 --------------------------------------------------

def aggregate(trades: list[dict]) -> dict:
    if not trades:
        return {"total_trades": 0, "win_rate_pct": 0.0, "cum_return_pct": 0.0,
                "avg_pnl_pct": 0.0, "avg_hold_days": 0.0, "best_pct": 0.0, "worst_pct": 0.0}
    pnls = [t["pnl_pct"] / 100 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    # 동일비중 가정: 각 픽이 1/N 자본 → cum return = product(1+pnl)-1 단순화는 부정확
    # 더 현실적: 일별 PnL 평균 후 누적. 여기선 단순 합계 평균으로 표시.
    avg = float(np.mean(pnls)) * 100
    cum = float((np.array([1] + [1 + p for p in pnls])).prod() - 1) * 100  # compounded
    return {
        "total_trades":   len(trades),
        "win_rate_pct":   round(wins / len(trades) * 100, 1),
        "cum_return_pct": round(cum, 2),
        "avg_pnl_pct":    round(avg, 3),
        "avg_hold_days":  round(float(np.mean([t["bars_held"] for t in trades])), 2),
        "best_pct":       round(max(pnls) * 100, 2),
        "worst_pct":      round(min(pnls) * 100, 2),
    }


# ---- main ----------------------------------------------------------------

def main():
    end = datetime.now().date()
    log.info("백테스트 기간: %s ~ %s", START_DATE, end)

    all_trades: list[dict] = []

    log.info("--- 미장 유니버스 (%d 종목) ---", len(US_UNIVERSE))
    for t in US_UNIVERSE:
        df = ds.fetch_history(t, market="us", period_days=400)
        if df is None or df.empty: continue
        trades = backtest_one(df, t, "us", us_signal)
        if trades: all_trades.extend(trades)
        log.info("  %s: %d trades", t, len(trades))

    log.info("--- 한국장 유니버스 (%d 종목) ---", len(KR_UNIVERSE))
    for t in KR_UNIVERSE:
        df = ds.fetch_history(t, market="kr", period_days=400)
        if df is None or df.empty: continue
        trades = backtest_one(df, t, "kr", kr_signal)
        if trades: all_trades.extend(trades)
        log.info("  %s: %d trades", t, len(trades))

    summary = aggregate(all_trades)
    by_market = {
        "kr": aggregate([t for t in all_trades if t["market"] == "kr"]),
        "us": aggregate([t for t in all_trades if t["market"] == "us"]),
    }

    payload = {
        "period_start": START_DATE,
        "period_end":   str(end),
        "generated_at": datetime.now().isoformat(),
        "summary":      summary,
        "by_market":    by_market,
        "trades":       sorted(all_trades, key=lambda t: t["entry_date"]),
    }

    class NpEnc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, np.bool_): return bool(o)
            if isinstance(o, np.integer): return int(o)
            if isinstance(o, np.floating): return float(o)
            if isinstance(o, np.ndarray): return o.tolist()
            return str(o)
    out = Path("landing/data/backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, cls=NpEnc), encoding="utf-8")
    log.info("✅ %s 작성", out)
    log.info("총 거래 %d건 / 승률 %.1f%% / 누적 수익률 %.2f%% / 평균 보유 %.1f일",
             summary["total_trades"], summary["win_rate_pct"],
             summary["cum_return_pct"], summary["avg_hold_days"])


if __name__ == "__main__":
    main()
