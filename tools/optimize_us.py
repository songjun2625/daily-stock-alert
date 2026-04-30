"""
US 알고리즘 파라미터 스윕 — 2월 약세장에서 -51% 손실 원인 해결.

진단:
  1월 +16% (13승/22) / 2월 -51% (5승/26) / 3월 -2% / 4월 +45%
  → 2월 약세장에서 잘못된 신호 폭증이 원인.

스윕 차원:
  1) Exit rules: stop × target × hold
  2) Entry signal: RSI 범위 / dd / breakout / vspike
  3) Market regime filter: SPY 50일선 위/아래 (약세장 보호)
  4) Trailing stop: off / be / lock-in
"""
from __future__ import annotations
import json, sys, time, logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import indicators as ind
from screener import data_sources as ds
from screener.screener_us import DEFAULT_UNIVERSE as US_UNIVERSE

log = logging.getLogger("opt_us")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

START_DATE = "2026-01-01"
COMM = 0.00025
SLIP = 0.001


def us_signal_variant(close: pd.Series, vol: pd.Series, idx: int,
                      spy_close: Optional[pd.Series],
                      rsi_low: float, rsi_high: float,
                      dd_low: float, dd_high: float,
                      use_breakout: bool,
                      vspike_mult: float,
                      regime_filter: str = "off",
                      regime_idx_to_spy_idx: Optional[dict] = None,
                      ) -> bool:
    """SPY regime 필터: 'off' = 항상 통과, 'spy_50ma' = SPY가 50일선 위에 있을 때만 진입.
    'spy_50ma_strict' = SPY 가 5%+ 50일선 위 + 5일선이 20일선 위."""
    if idx < 60: return False
    sub_close = close.iloc[: idx + 1]
    sub_vol   = vol.iloc[: idx + 1]
    rsi_v = float(ind.rsi(sub_close).iloc[-1])
    macd_l, sig_l, _ = ind.macd(sub_close)
    ma_up_now = ind.is_ma_aligned_up(sub_close)
    peak = sub_close.tail(252).max()
    dd = float((peak - sub_close.iloc[-1]) / peak) if peak > 0 else 0
    cheap = (rsi_low <= rsi_v <= rsi_high) or (dd_low <= dd <= dd_high)
    if use_breakout and dd <= 0.05 and ma_up_now:
        cheap = True
    if not cheap: return False
    entry = (ind.is_macd_golden_cross(macd_l, sig_l)
             or ma_up_now
             or ind.volume_spike(sub_vol, multiplier=vspike_mult))
    if not entry: return False

    # SPY regime 필터
    if regime_filter != "off" and spy_close is not None:
        # 종목 시계열 idx를 SPY idx 로 매핑
        cur_date = close.index[idx]
        spy_loc = spy_close.index.searchsorted(cur_date)
        if spy_loc >= len(spy_close): spy_loc = len(spy_close) - 1
        spy_sub = spy_close.iloc[: spy_loc + 1]
        if len(spy_sub) < 50: return False
        spy_ma50 = spy_sub.rolling(50).mean().iloc[-1]
        spy_cur  = spy_sub.iloc[-1]
        if regime_filter == "spy_50ma":
            if spy_cur < spy_ma50: return False
        elif regime_filter == "spy_50ma_strict":
            spy_ma5  = spy_sub.rolling(5).mean().iloc[-1]
            spy_ma20 = spy_sub.rolling(20).mean().iloc[-1]
            if spy_cur < spy_ma50: return False
            if spy_ma5 < spy_ma20: return False
    return True


def backtest_one(history: pd.DataFrame, ticker: str, signal_fn,
                 stop_pct: float, target_pct: float, hold_days: int,
                 trailing: str = "off") -> list[dict]:
    if history is None or len(history) < 80: return []
    close = history["Close"]; vol = history["Volume"]
    high  = history["High"];  low = history["Low"]
    try: opens = history["Open"]
    except KeyError: opens = close
    start = pd.Timestamp(START_DATE, tz=close.index.tz) if close.index.tz else pd.Timestamp(START_DATE)
    trades: list[dict] = []
    cooldown = 0
    i = max(60, close.index.searchsorted(start) - 1)
    while i < len(close) - 1:
        if cooldown > 0:
            cooldown -= 1; i += 1; continue
        if close.index[i] < start:
            i += 1; continue
        if not signal_fn(close, vol, i):
            i += 1; continue
        entry_idx = i + 1
        if entry_idx >= len(close): break
        entry_open = float(opens.iloc[entry_idx])
        entry_px = entry_open * (1 + SLIP)
        target_px = entry_px * (1 + target_pct)
        stop_px   = entry_px * (1 - stop_pct)
        exit_idx = None; exit_px = None; reason = "time"
        for j in range(entry_idx, min(entry_idx + hold_days, len(close))):
            day_high = float(high.iloc[j]); day_low = float(low.iloc[j])
            if trailing == "be_at_+3" and day_high >= entry_px * 1.03:
                stop_px = max(stop_px, entry_px)
            elif trailing == "trail_+3_at_+6" and day_high >= entry_px * 1.06:
                stop_px = max(stop_px, entry_px * 1.03)
            if day_low <= stop_px:
                exit_idx, exit_px, reason = j, stop_px * (1 - SLIP), "stop"; break
            if day_high >= target_px:
                exit_idx, exit_px, reason = j, target_px * (1 - SLIP), "target"; break
        if exit_idx is None:
            exit_idx = min(entry_idx + hold_days - 1, len(close) - 1)
            exit_px  = float(close.iloc[exit_idx]) * (1 - SLIP)
            reason   = "time"
        gross = (exit_px - entry_px) / entry_px
        net = gross - 2 * COMM
        trades.append({
            "ticker": ticker,
            "entry_date": str(close.index[entry_idx].date()),
            "exit_date":  str(close.index[exit_idx].date()),
            "bars_held":  exit_idx - entry_idx + 1,
            "pnl_pct":    round(net * 100, 3),
            "reason":     reason,
        })
        cooldown = exit_idx - i + 1
        i = exit_idx + 1
    return trades


def evaluate(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win": 0.0, "cum": 0.0, "avg": 0.0, "hold": 0.0, "max_dd": 0.0}
    pnls = [t["pnl_pct"] / 100 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    # 누적 곡선과 MDD
    cum_curve = np.cumprod([1 + p for p in pnls])
    cum_max = np.maximum.accumulate(cum_curve)
    drawdown = (cum_curve - cum_max) / cum_max
    return {
        "trades": len(trades),
        "win": round(wins / len(trades) * 100, 1),
        "cum": round((float(cum_curve[-1]) - 1) * 100, 2),
        "avg": round(float(np.mean(pnls)) * 100, 3),
        "hold": round(float(np.mean([t["bars_held"] for t in trades])), 1),
        "max_dd": round(float(drawdown.min()) * 100, 2),
    }


# 데이터 로드
print(f"📥 US 유니버스 {len(US_UNIVERSE)} 종목 + SPY 데이터 로드...")
data_cache: dict[str, pd.DataFrame] = {}
for t in US_UNIVERSE:
    df = ds.fetch_history(t, market="us", period_days=400)
    if df is not None and not df.empty and len(df) >= 80:
        data_cache[t] = df
spy_df = ds.fetch_history("SPY", market="us", period_days=400)
spy_close = spy_df["Close"] if spy_df is not None and not spy_df.empty else None
if spy_close is not None and spy_close.index.tz is not None:
    spy_close.index = spy_close.index.tz_localize(None)
for t, df in data_cache.items():
    if df.index.tz is not None:
        data_cache[t] = df.tz_localize(None)
print(f"   {len(data_cache)} 종목 + SPY {'OK' if spy_close is not None else 'MISSING'}\n")


def run_config(name: str, **kwargs) -> dict:
    rsi_low = kwargs.get("rsi_low", 30)
    rsi_high = kwargs.get("rsi_high", 45)
    dd_low = kwargs.get("dd_low", 0.15)
    dd_high = kwargs.get("dd_high", 0.40)
    use_breakout = kwargs.get("breakout", False)
    vspike_mult = kwargs.get("vspike_mult", 2.0)
    stop = kwargs.get("stop", 0.04)
    target = kwargs.get("target", 0.06)
    hold = kwargs.get("hold", 5)
    trailing = kwargs.get("trailing", "off")
    regime = kwargs.get("regime", "off")

    def signal(c, v, i):
        return us_signal_variant(c, v, i, spy_close,
                                 rsi_low, rsi_high, dd_low, dd_high,
                                 use_breakout, vspike_mult, regime)

    all_trades: list[dict] = []
    for t, df in data_cache.items():
        trades = backtest_one(df, t, signal, stop, target, hold, trailing)
        all_trades.extend(trades)

    metrics = evaluate(all_trades)
    return {"name": name, **kwargs, **metrics}


configs = [
    # === 베이스라인 (현재 운영) ===
    dict(name="A_base",            rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.04, target=0.06, hold=5, trailing="off", regime="off"),

    # === SPY regime 필터 추가 (2월 약세장 차단) ===
    dict(name="B_spy_50ma",        rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.04, target=0.06, hold=5, trailing="off", regime="spy_50ma"),
    dict(name="C_spy_strict",      rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.04, target=0.06, hold=5, trailing="off", regime="spy_50ma_strict"),

    # === Exit rules 조정 ===
    dict(name="D_loose_exit",      rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.05, target=0.08, hold=7, trailing="off", regime="spy_50ma"),
    dict(name="E_tight_stop",      rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.03, target=0.05, hold=5, trailing="off", regime="spy_50ma"),

    # === RSI 좁힘 (질 높이기) ===
    dict(name="F_rsi_30_40",       rsi_low=30, rsi_high=40, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.04, target=0.06, hold=5, trailing="off", regime="spy_50ma"),

    # === Breakout 추가 ===
    dict(name="G_with_breakout",   rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=True,  vspike_mult=2.0, stop=0.04, target=0.06, hold=5, trailing="off", regime="spy_50ma"),

    # === regime + breakout + 장기 보유 ===
    dict(name="H_combo",           rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=True,  vspike_mult=1.5, stop=0.04, target=0.08, hold=7, trailing="off", regime="spy_50ma"),

    # === Trailing ===
    dict(name="I_trail_be",        rsi_low=30, rsi_high=45, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.04, target=0.08, hold=7, trailing="be_at_+3", regime="spy_50ma"),

    # === 매우 보수 ===
    dict(name="J_strict",          rsi_low=30, rsi_high=40, dd_low=0.20, dd_high=0.35, breakout=False, vspike_mult=2.5, stop=0.04, target=0.06, hold=5, trailing="off", regime="spy_50ma_strict"),

    # === 모멘텀 only (RSI 사용 안 함, breakout만) ===
    dict(name="K_momentum",        rsi_low=99, rsi_high=99, dd_low=0.99, dd_high=0.99, breakout=True,  vspike_mult=1.5, stop=0.04, target=0.08, hold=7, trailing="off", regime="spy_50ma"),

    # === regime + tighter exit ===
    dict(name="L_regime_tight",    rsi_low=30, rsi_high=42, dd_low=0.15, dd_high=0.40, breakout=False, vspike_mult=2.0, stop=0.035, target=0.06, hold=5, trailing="off", regime="spy_50ma"),
]

print(f"🧪 {len(configs)}개 조합 백테스트\n")
results = []
for cfg in configs:
    t0 = time.time()
    r = run_config(**cfg)
    dt = time.time() - t0
    results.append(r)
    sign = "+" if r["cum"] >= 0 else ""
    print(f"  {r['name']:18s} trades={r['trades']:>3} win={r['win']:>4.1f}% cum={sign}{r['cum']:>7.2f}% mdd={r['max_dd']:>6.1f}% avg={r['avg']:+.2f}% ({dt:.0f}s)")

print("\n" + "=" * 90)
print("🏆 정렬 (cum_return 내림차순)")
print("=" * 90)
results.sort(key=lambda r: r["cum"], reverse=True)
print(f"{'name':<18} {'설정':<60} {'거래':>4} {'승률':>5} {'누적':>8} {'MDD':>6}")
for r in results:
    cfg_str = (f"R{r['rsi_low']}~{r['rsi_high']} "
               f"dd{r['dd_low']*100:.0f}~{r['dd_high']*100:.0f} "
               f"{'+brk' if r['breakout'] else '   '} "
               f"v{r['vspike_mult']:.1f} "
               f"{r['stop']*100:.1f}/{r['target']*100:.1f}/{r['hold']}d "
               f"{r['trailing']:<14} {r['regime']}")
    sign = "+" if r["cum"] >= 0 else ""
    print(f"{r['name']:<18} {cfg_str:<60} {r['trades']:>4} {r['win']:>4.1f}% {sign}{r['cum']:>6.2f}% {r['max_dd']:>5.1f}%")

out = Path(__file__).parent / "us_optimization_report.json"
out.write_text(json.dumps({"start_date": START_DATE, "results": results},
                          ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n저장: {out}")

best = results[0]
print("\n" + "=" * 90)
print(f"✨ 최적 조합: {best['name']}")
print("=" * 90)
print(f"  RSI:        {best['rsi_low']} ~ {best['rsi_high']}")
print(f"  DD:         {best['dd_low']*100:.0f}% ~ {best['dd_high']*100:.0f}%")
print(f"  Breakout:   {'사용' if best['breakout'] else '미사용'}")
print(f"  vspike:     {best['vspike_mult']:.1f}x")
print(f"  Exit:       -{best['stop']*100:.1f}% / +{best['target']*100:.1f}% / {best['hold']}일")
print(f"  Trailing:   {best['trailing']}")
print(f"  Regime:     {best['regime']}")
print(f"  → 거래 {best['trades']}건, 승률 {best['win']:.1f}%, 누적 {best['cum']:+.2f}%, MDD {best['max_dd']:.1f}%")
