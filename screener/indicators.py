"""
가격 시계열 지표 — RSI, MACD, 이동평균.
의존: pandas, numpy 만 사용. yfinance/FDR 등 데이터 소스에 독립.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def is_macd_golden_cross(macd_line: pd.Series, signal_line: pd.Series) -> bool:
    if len(macd_line) < 2 or len(signal_line) < 2:
        return False
    prev_diff = macd_line.iloc[-2] - signal_line.iloc[-2]
    cur_diff  = macd_line.iloc[-1] - signal_line.iloc[-1]
    return prev_diff <= 0 < cur_diff


def is_ma_aligned_up(close: pd.Series, fast: int = 5, slow: int = 20) -> bool:
    """5일선 > 20일선 정배열 시작 여부."""
    if len(close) < slow + 1:
        return False
    ma_fast = close.rolling(fast).mean()
    ma_slow = close.rolling(slow).mean()
    return bool(ma_fast.iloc[-1] > ma_slow.iloc[-1] and ma_fast.iloc[-2] <= ma_slow.iloc[-2] * 1.001)


def volume_spike(volume: pd.Series, lookback: int = 5, multiplier: float = 2.0) -> bool:
    if len(volume) < lookback + 1:
        return False
    avg = volume.iloc[-lookback - 1:-1].mean()
    return bool(volume.iloc[-1] >= avg * multiplier)


def drawdown_from_52w_high(close: pd.Series) -> float:
    """52주 최고가 대비 현재가 하락률(양수). 0.25 = -25%."""
    window = close.tail(252) if len(close) >= 252 else close
    peak = window.max()
    return float((peak - close.iloc[-1]) / peak) if peak > 0 else 0.0
