"""
KR 알고리즘 파라미터 스윕 — 손절·목표·보유·신호 조합을 동시에 테스트해 최적안 찾기.

스윕 차원:
  1) Exit rules: stop_pct × target_pct × hold_days
  2) Entry signal: RSI 영역 / 드로우다운 / breakout / momentum 4가지 조합
  3) Trailing stop: off / breakeven_at +3% / move_to_+3_at_+6

대안 비교 평가:
  - KR-only 누적 수익률, 승률, MDD, 평균 보유, Sharpe-like
  - 거래 수가 너무 많거나 너무 적으면 페널티

전체 sweep ≈ 32~64 조합. 각 조합마다 KR 50종목 ×120일 백테스트 ≈ 30초.
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
from screener.screener_kr import DEFAULT_KR_UNIVERSE as KR_UNIVERSE

log = logging.getLogger("opt_kr")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

START_DATE = "2026-01-01"
COMM = 0.00025
SLIP = 0.001


def _atr(close: pd.Series, high: pd.Series, low: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) < period + 1: return None
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def kr_signal_variant(close: pd.Series, vol: pd.Series, idx: int,
                      rsi_low: float, rsi_high: float,
                      use_breakout: bool, use_deep_dd: bool,
                      vspike_mult: float) -> bool:
    if idx < 60: return False
    sub_close = close.iloc[: idx + 1]
    sub_vol   = vol.iloc[: idx + 1]
    rsi_v = float(ind.rsi(sub_close).iloc[-1])
    macd_l, sig_l, _ = ind.macd(sub_close)
    ma_up_now = ind.is_ma_aligned_up(sub_close)
    peak = sub_close.tail(252).max()
    dd = float((peak - sub_close.iloc[-1]) / peak) if peak > 0 else 0
    cheap = rsi_low <= rsi_v <= rsi_high
    if use_deep_dd and dd >= 0.30:
        cheap = True
    if use_breakout and dd <= 0.05 and ma_up_now:
        cheap = True
    if not cheap: return False
    entry = (ind.is_macd_golden_cross(macd_l, sig_l)
             or ma_up_now
             or ind.volume_spike(sub_vol, multiplier=vspike_mult))
    return bool(entry)


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
            day_close = float(close.iloc[j])
            # 트레일링 스톱
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
        return {"trades": 0, "win": 0.0, "cum": 0.0, "avg": 0.0, "hold": 0.0}
    pnls = [t["pnl_pct"] / 100 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    cum = float(np.prod([1 + p for p in pnls]) - 1) * 100
    return {
        "trades": len(trades),
        "win": round(wins / len(trades) * 100, 1),
        "cum": round(cum, 2),
        "avg": round(float(np.mean(pnls)) * 100, 3),
        "hold": round(float(np.mean([t["bars_held"] for t in trades])), 1),
    }


# 유니버스 데이터 미리 로드 — 한 번만 다운로드, 모든 조합에 재사용
print(f"📥 KR 유니버스 {len(KR_UNIVERSE)} 종목 데이터 로드 중...")
data_cache: dict[str, pd.DataFrame] = {}
for t in KR_UNIVERSE:
    df = ds.fetch_history(t, market="kr", period_days=400)
    if df is not None and not df.empty and len(df) >= 80:
        data_cache[t] = df
print(f"   {len(data_cache)} 종목 로드 완료\n")


def run_config(name: str, **kwargs) -> dict:
    rsi_low = kwargs.get("rsi_low", 30)
    rsi_high = kwargs.get("rsi_high", 35)
    use_breakout = kwargs.get("breakout", False)
    use_deep_dd = kwargs.get("deep_dd", False)
    vspike_mult = kwargs.get("vspike_mult", 2.0)
    stop = kwargs.get("stop", 0.025)
    target = kwargs.get("target", 0.04)
    hold = kwargs.get("hold", 5)
    trailing = kwargs.get("trailing", "off")

    def signal(c, v, i):
        return kr_signal_variant(c, v, i, rsi_low, rsi_high, use_breakout, use_deep_dd, vspike_mult)

    all_trades: list[dict] = []
    for t, df in data_cache.items():
        trades = backtest_one(df, t, signal, stop, target, hold, trailing)
        all_trades.extend(trades)

    metrics = evaluate(all_trades)
    return {"name": name, **kwargs, **metrics}


# 실험 설계 — 차원별 조합 (모든 곱 64 조합은 너무 많음, 의미있는 후보만)
configs = [
    # === 베이스라인 (현재 운영) ===
    dict(name="A_base",          rsi_low=30, rsi_high=35, deep_dd=True,  breakout=False, vspike_mult=2.0, stop=0.025, target=0.04, hold=5, trailing="off"),

    # === Exit rules 완화 ===
    dict(name="B_loose_exit",    rsi_low=30, rsi_high=35, deep_dd=True,  breakout=False, vspike_mult=2.0, stop=0.035, target=0.06, hold=7, trailing="off"),
    dict(name="C_very_loose",    rsi_low=30, rsi_high=35, deep_dd=True,  breakout=False, vspike_mult=2.0, stop=0.04,  target=0.08, hold=10, trailing="off"),
    dict(name="D_us_style",      rsi_low=30, rsi_high=35, deep_dd=True,  breakout=False, vspike_mult=2.0, stop=0.04,  target=0.06, hold=5, trailing="off"),

    # === RSI 범위 확장 ===
    dict(name="E_rsi_wide",      rsi_low=30, rsi_high=40, deep_dd=True,  breakout=False, vspike_mult=1.5, stop=0.035, target=0.06, hold=7, trailing="off"),
    dict(name="F_rsi_30_45",     rsi_low=30, rsi_high=45, deep_dd=True,  breakout=False, vspike_mult=1.5, stop=0.035, target=0.06, hold=7, trailing="off"),

    # === Breakout pattern 추가 ===
    dict(name="G_with_breakout", rsi_low=30, rsi_high=35, deep_dd=True,  breakout=True,  vspike_mult=1.5, stop=0.035, target=0.06, hold=7, trailing="off"),
    dict(name="H_breakout_wide", rsi_low=30, rsi_high=40, deep_dd=True,  breakout=True,  vspike_mult=1.5, stop=0.035, target=0.06, hold=7, trailing="off"),

    # === Trailing stops ===
    dict(name="I_trail_be",      rsi_low=30, rsi_high=40, deep_dd=True,  breakout=True,  vspike_mult=1.5, stop=0.035, target=0.08, hold=10, trailing="be_at_+3"),
    dict(name="J_trail_lock",    rsi_low=30, rsi_high=40, deep_dd=True,  breakout=True,  vspike_mult=1.5, stop=0.035, target=0.10, hold=10, trailing="trail_+3_at_+6"),

    # === 보수적 (선별 강화) ===
    dict(name="K_strict",        rsi_low=30, rsi_high=33, deep_dd=False, breakout=False, vspike_mult=2.5, stop=0.035, target=0.06, hold=7, trailing="off"),

    # === 모멘텀 강조 (정배열 + breakout 만) ===
    dict(name="L_momentum_only", rsi_low=99, rsi_high=99, deep_dd=False, breakout=True,  vspike_mult=1.5, stop=0.035, target=0.08, hold=10, trailing="be_at_+3"),
]

print(f"🧪 {len(configs)}개 조합 백테스트 시작 (예상 5~10분)\n")
results = []
for cfg in configs:
    t0 = time.time()
    r = run_config(**cfg)
    dt = time.time() - t0
    results.append(r)
    sign = "+" if r["cum"] >= 0 else ""
    print(f"  {r['name']:18s}  trades={r['trades']:>3} win={r['win']:>4.1f}% cum={sign}{r['cum']:>7.2f}% avg={r['avg']:+.2f}% hold={r['hold']:.1f}일  ({dt:.0f}s)")

print("\n" + "=" * 80)
print("🏆 정렬 (cum_return 내림차순)")
print("=" * 80)
print(f"{'name':<18} {'설정':<55} {'거래':>4} {'승률':>5} {'누적':>8} {'평균':>7}")
results.sort(key=lambda r: r["cum"], reverse=True)
for r in results:
    cfg_str = (f"RSI {r['rsi_low']}~{r['rsi_high']} "
               f"{'+dd' if r['deep_dd'] else '   '} "
               f"{'+brk' if r['breakout'] else '    '} "
               f"vsp{r['vspike_mult']:.1f} "
               f"{r['stop']*100:.1f}/{r['target']*100:.1f}/{r['hold']}d "
               f"{r['trailing']}")
    sign = "+" if r["cum"] >= 0 else ""
    print(f"{r['name']:<18} {cfg_str:<55} {r['trades']:>4} {r['win']:>4.1f}% "
          f"{sign}{r['cum']:>6.2f}% {r['avg']:+6.2f}%")

# JSON 저장
out = Path(__file__).parent / "kr_optimization_report.json"
out.write_text(json.dumps({"start_date": START_DATE, "results": results},
                          ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n저장: {out}")

# Top 1 추천
best = results[0]
print("\n" + "=" * 80)
print(f"✨ 최적 조합: {best['name']}")
print("=" * 80)
print(f"  RSI 범위:    {best['rsi_low']} ~ {best['rsi_high']}")
print(f"  깊은 DD 30%+: {'사용' if best['deep_dd'] else '미사용'}")
print(f"  Breakout:    {'사용' if best['breakout'] else '미사용'}")
print(f"  거래량 배수:  {best['vspike_mult']:.1f}x")
print(f"  손절:        -{best['stop']*100:.1f}%")
print(f"  목표:        +{best['target']*100:.1f}%")
print(f"  최대 보유:    {best['hold']}일")
print(f"  Trailing:    {best['trailing']}")
print(f"  → 거래 {best['trades']}건, 승률 {best['win']:.1f}%, 누적 {best['cum']:+.2f}%, 평균 {best['avg']:+.2f}%")
