"""
백테스트 — 스크리너가 실제로 작동했는지 검증.

원칙:
  - 룩어헤드 바이어스 방지: t일 시그널은 t일 종가까지의 데이터로만 계산, t+1일 시가에 매수.
  - 거래비용: 슬리피지 0.1% + 수수료 0.025% (양방향 적용).
  - 보유 기간: 단기 스윙 5일 고정 (또는 손절·목표 도달 시 조기 청산).
  - 동일 비중 (각 픽 1/N).
  - 결과: 누적 수익률, 승률, 평균 보유, 최대 드로우다운(MDD), 샤프 비율.

⚠️ 백테스트 결과는 과거 시점 시뮬레이션이며, 실제 투자 환경(체결가능성·세금·환차손)과 차이가 있습니다.
   본 결과는 마케팅에 "수익 보장" "확정 수익률" 등으로 절대 사용 금지.
"""
from __future__ import annotations
import logging, math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import indicators as ind

log = logging.getLogger(__name__)

COMMISSION_BPS = 2.5    # 0.025%
SLIPPAGE_BPS   = 10.0   # 0.1%
HOLD_DAYS_MAX  = 5
STOP_PCT       = 0.04   # -4%
TARGET_PCT     = 0.06   # +6% (단순 룰)


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    bars_held: int
    pnl_pct: float
    reason: str          # 'target' / 'stop' / 'time'


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)

    @property
    def n_trades(self) -> int: return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades: return 0.0
        return sum(1 for t in self.trades if t.pnl_pct > 0) / len(self.trades)

    @property
    def avg_pnl(self) -> float:
        if not self.trades: return 0.0
        return float(np.mean([t.pnl_pct for t in self.trades]))

    @property
    def avg_hold(self) -> float:
        if not self.trades: return 0.0
        return float(np.mean([t.bars_held for t in self.trades]))

    @property
    def cumulative_return(self) -> float:
        if self.equity_curve.empty: return 0.0
        return float(self.equity_curve.iloc[-1] - 1.0)

    @property
    def mdd(self) -> float:
        if self.equity_curve.empty: return 0.0
        roll_max = self.equity_curve.cummax()
        dd = self.equity_curve / roll_max - 1.0
        return float(dd.min())

    @property
    def sharpe(self) -> float:
        if self.equity_curve.empty or len(self.equity_curve) < 30: return 0.0
        rets = self.equity_curve.pct_change().dropna()
        if rets.std() == 0: return 0.0
        return float(rets.mean() / rets.std() * math.sqrt(252))

    def summary(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate * 100, 2),
            "avg_pnl_pct": round(self.avg_pnl * 100, 3),
            "avg_hold_days": round(self.avg_hold, 2),
            "cumulative_return_pct": round(self.cumulative_return * 100, 2),
            "max_drawdown_pct": round(self.mdd * 100, 2),
            "sharpe": round(self.sharpe, 2),
        }


# ---- 시그널 함수 (스크리너와 동일 로직, 룩어헤드 방지) ----------------------

def _is_signal(close: pd.Series, vol: pd.Series, idx: int,
               rsi_low: float = 30, rsi_high: float = 45) -> bool:
    """idx 시점의 시그널 — idx까지의 데이터로만 계산."""
    if idx < 60: return False
    sub_close = close.iloc[: idx + 1]
    sub_vol   = vol.iloc[: idx + 1]
    rsi_v = float(ind.rsi(sub_close).iloc[-1])
    if not (rsi_low <= rsi_v <= rsi_high): return False
    macd_l, sig_l, _ = ind.macd(sub_close)
    if not ind.is_macd_golden_cross(macd_l, sig_l) and not ind.is_ma_aligned_up(sub_close):
        return False
    if not ind.volume_spike(sub_vol, multiplier=2.0): return False
    return True


# ---- 단일 종목 백테스트 ---------------------------------------------------

def backtest_ticker(history: pd.DataFrame, ticker: str = "?") -> list[Trade]:
    if history.empty or len(history) < 80:
        return []
    close = history["Close"]
    vol = history["Volume"]
    high = history["High"]
    low = history["Low"]

    trades: list[Trade] = []
    cooldown = 0
    i = 60
    while i < len(close) - 1:
        if cooldown > 0:
            cooldown -= 1; i += 1; continue
        if not _is_signal(close, vol, i):
            i += 1; continue

        # t+1일 시가에 진입
        entry_idx = i + 1
        if entry_idx >= len(close): break
        entry_open = float(history["Open"].iloc[entry_idx])
        entry_price = entry_open * (1 + SLIPPAGE_BPS / 1e4)
        target = entry_price * (1 + TARGET_PCT)
        stop   = entry_price * (1 - STOP_PCT)

        exit_idx, exit_price, reason = None, None, "time"
        for j in range(entry_idx, min(entry_idx + HOLD_DAYS_MAX, len(close))):
            day_high = float(high.iloc[j]); day_low = float(low.iloc[j])
            if day_low <= stop:
                exit_idx, exit_price, reason = j, stop * (1 - SLIPPAGE_BPS / 1e4), "stop"; break
            if day_high >= target:
                exit_idx, exit_price, reason = j, target * (1 - SLIPPAGE_BPS / 1e4), "target"; break
        if exit_idx is None:
            exit_idx = min(entry_idx + HOLD_DAYS_MAX - 1, len(close) - 1)
            exit_price = float(close.iloc[exit_idx]) * (1 - SLIPPAGE_BPS / 1e4)
            reason = "time"

        # 양방향 수수료
        gross = (exit_price - entry_price) / entry_price
        net = gross - 2 * COMMISSION_BPS / 1e4
        trades.append(Trade(
            ticker=ticker,
            entry_date=close.index[entry_idx], entry_price=entry_price,
            exit_date=close.index[exit_idx],   exit_price=exit_price,
            bars_held=exit_idx - entry_idx + 1,
            pnl_pct=float(net), reason=reason,
        ))
        cooldown = exit_idx - i + 1   # 동일 종목 즉시 재진입 방지
        i = exit_idx + 1
    return trades


# ---- 포트폴리오 백테스트 (다종목 동일비중) --------------------------------

def backtest_portfolio(histories: dict[str, pd.DataFrame]) -> BacktestResult:
    """histories: {ticker: DataFrame(Open/High/Low/Close/Volume, datetime index)}"""
    all_trades: list[Trade] = []
    daily_pnl: dict[pd.Timestamp, list[float]] = {}

    for ticker, hist in histories.items():
        trades = backtest_ticker(hist, ticker)
        all_trades.extend(trades)
        for t in trades:
            # 보유 일수에 PnL 균등 분배 (단순화 — equity curve 부드럽게)
            daily = t.pnl_pct / max(t.bars_held, 1)
            for d in pd.date_range(t.entry_date, t.exit_date, freq="B"):
                daily_pnl.setdefault(d, []).append(daily)

    if not daily_pnl:
        return BacktestResult(trades=all_trades, equity_curve=pd.Series([1.0]))

    dates = sorted(daily_pnl.keys())
    daily_avg = pd.Series([np.mean(daily_pnl[d]) for d in dates], index=dates)
    equity = (1 + daily_avg).cumprod()
    return BacktestResult(trades=all_trades, equity_curve=equity)


# ---- CLI ----------------------------------------------------------------

def run_demo(market: str = "us", years: float = 2.0) -> dict:
    """기본 유니버스로 백테스트 데모 실행. 결과 dict 반환 + 콘솔 출력."""
    end = datetime.now().date()
    start = end - timedelta(days=int(365 * years))

    histories: dict[str, pd.DataFrame] = {}
    if market == "us":
        from .screener_us import DEFAULT_UNIVERSE
        import yfinance as yf
        for t in DEFAULT_UNIVERSE[:15]:   # 데모는 15개로 제한
            try:
                df = yf.Ticker(t).history(start=start, end=end, auto_adjust=True)
                if not df.empty: histories[t] = df
            except Exception as e:
                log.warning("fetch %s failed: %s", t, e)
    else:
        from .screener_kr import DEFAULT_KR_UNIVERSE
        try:
            import FinanceDataReader as fdr
            for t in DEFAULT_KR_UNIVERSE[:15]:
                df = fdr.DataReader(t, start, end)
                df.columns = [c.capitalize() for c in df.columns]
                if not df.empty: histories[t] = df
        except Exception as e:
            log.warning("fdr fetch failed: %s", e)

    res = backtest_portfolio(histories)
    summary = res.summary()
    summary["period"] = f"{start} ~ {end}"
    summary["universe_size"] = len(histories)
    return summary


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    market = sys.argv[1] if len(sys.argv) > 1 else "us"
    years = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    print(run_demo(market=market, years=years))
