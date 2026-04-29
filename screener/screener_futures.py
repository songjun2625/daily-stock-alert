"""
선물·ETF 스크리너 — Pro 플랜 회원 대상.

다루는 자산:
  국내 ETF: KODEX 200, TIGER 미국나스닥100, KODEX 코스닥150 레버리지 등
  국내 선물: KOSPI 200 선물 (^KS200) — 일별 데이터만, 분봉은 키움/KIS API 필요
  미국 ETF: SPY, QQQ, TQQQ 등 (us-swing-screener 스킬 스펙의 레버리지 옵션)

레버리지 ETF는 스킬 스펙에 따라:
  - 포지션 사이즈 일반 종목의 75% (~300만원 / 전체의 15%)
  - 손절폭 더 타이트 (-3%, RR 1:1.5)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from . import indicators as ind
from . import data_sources as ds
from .config import KST

log = logging.getLogger(__name__)

# 선물·ETF 유니버스 — 거래량 큰 핵심만.
DEFAULT_FUTURES_UNIVERSE = [
    # 한국 ETF
    {"ticker": "069500", "name": "KODEX 200",          "market": "kr", "leveraged": False},
    {"ticker": "229200", "name": "KODEX 코스닥150",    "market": "kr", "leveraged": False},
    {"ticker": "133690", "name": "TIGER 미국나스닥100","market": "kr", "leveraged": False},
    {"ticker": "381180", "name": "TIGER 미국필라델피아반도체","market":"kr", "leveraged": False},
    {"ticker": "122630", "name": "KODEX 레버리지",     "market": "kr", "leveraged": True},
    {"ticker": "252670", "name": "KODEX 200선물인버스2X","market":"kr", "leveraged": True},

    # 미국 ETF
    {"ticker": "SPY",  "name": "SPDR S&P 500",          "market": "us", "leveraged": False},
    {"ticker": "QQQ",  "name": "Invesco QQQ Nasdaq 100","market": "us", "leveraged": False},
    {"ticker": "TQQQ", "name": "ProShares UltraPro QQQ (3x)","market":"us","leveraged": True},
    {"ticker": "SOXL", "name": "Direxion Semiconductors 3x", "market":"us","leveraged": True},
]


@dataclass
class FuturesCandidate:
    ticker: str
    name: str
    market: str               # 'kr' or 'us'
    leveraged: bool
    price: float
    price_krw: float          # 원화 환산 (미국 ETF에만 의미)
    change_pct_1d: float
    rsi: float
    macd_golden_cross: bool
    ma_aligned_up: bool
    volume_spike: bool
    drawdown_52w: float
    score: float = 0.0
    reasons: list = field(default_factory=list)
    entry_low: float = 0.0
    entry_high: float = 0.0
    target: float = 0.0
    stoploss: float = 0.0
    hold_days_max: int = 5
    position_size_pct: float = 20.0       # 전체 자산 대비 권장 비중 (%)


def _atr(hist: pd.DataFrame, period: int = 14) -> Optional[float]:
    if hist is None or len(hist) < period + 1:
        return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _trade_plan(price: float, atr: Optional[float], leveraged: bool) -> tuple[float,float,float,float]:
    """레버리지 ETF는 더 타이트한 손절(-3% / RR 1:1.5), 일반은 -4% / RR 1:1.8."""
    if atr and atr > 0:
        stop_mult = 1.0 if leveraged else 1.5
        rr = 1.5 if leveraged else 1.8
        entry_low  = round(price - 0.5 * atr, 2)
        entry_high = round(price + 0.4 * atr, 2)
        stoploss   = round(price - stop_mult * atr, 2)
        risk = price - stoploss
        target = round(price + rr * risk, 2)
    else:
        if leveraged:
            entry_low, entry_high, stoploss = round(price * 0.99, 2), round(price * 1.005, 2), round(price * 0.97, 2)
            target = round(price * 1.045, 2)
        else:
            entry_low, entry_high, stoploss = round(price * 0.985, 2), round(price * 1.005, 2), round(price * 0.96, 2)
            target = round(price * 1.06, 2)
    return entry_low, entry_high, target, stoploss


def _score(c: FuturesCandidate) -> float:
    s = 0.0
    if 30 <= c.rsi <= 50: s += 25
    if c.macd_golden_cross: s += 20
    if c.ma_aligned_up: s += 15
    if c.volume_spike: s += 15
    if 0.05 <= c.drawdown_52w <= 0.30: s += 10
    if c.leveraged: s -= 5  # 변동성 페널티
    return round(max(s, 0), 2)


def screen_futures(universe: Iterable[dict] = DEFAULT_FUTURES_UNIVERSE) -> list[FuturesCandidate]:
    fx = ds.usd_krw()
    cands: list[FuturesCandidate] = []
    for entry in universe:
        ticker, name, market, lev = entry["ticker"], entry["name"], entry["market"], entry["leveraged"]
        hist = ds.fetch_history(ticker, market=market, period_days=200)
        if hist is None or hist.empty or len(hist) < 30:
            log.debug("skip %s — no data", ticker); continue

        close = hist["Close"]; vol = hist.get("Volume", pd.Series([0]*len(hist), index=hist.index))
        rsi_v = float(ind.rsi(close).iloc[-1])
        macd_l, sig_l, _ = ind.macd(close)
        gx = ind.is_macd_golden_cross(macd_l, sig_l)
        ma_up = ind.is_ma_aligned_up(close)
        vspike = ind.volume_spike(vol)
        dd = ind.drawdown_from_52w_high(close)
        atr = _atr(hist)
        price = float(close.iloc[-1])
        chg_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) >= 2 else 0.0

        # 게이트 완화: RSI 또는 진입 신호 중 1개만 충족해도 후보.
        if not (30 <= rsi_v <= 65 or gx or ma_up or vspike):
            continue

        reasons = []
        if 30 <= rsi_v <= 50: reasons.append(f"RSI {rsi_v:.0f}")
        if gx: reasons.append("MACD 골든크로스")
        if ma_up: reasons.append("5/20일선 정배열 시작")
        if vspike: reasons.append("거래량 5일 평균 2배+")
        if 0.05 <= dd <= 0.30: reasons.append(f"52주 고점 대비 -{dd*100:.0f}%")
        if lev: reasons.append("⚠️ 레버리지 ETF — 손실 2~3배 확대 주의")

        e_lo, e_hi, tgt, stop = _trade_plan(price, atr, lev)
        c = FuturesCandidate(
            ticker=ticker, name=name, market=market, leveraged=lev,
            price=price, price_krw=round(price * fx) if market == "us" else int(price),
            change_pct_1d=chg_1d, rsi=rsi_v,
            macd_golden_cross=gx, ma_aligned_up=ma_up,
            volume_spike=vspike, drawdown_52w=dd,
            reasons=reasons,
            entry_low=e_lo, entry_high=e_hi, target=tgt, stoploss=stop,
            position_size_pct=15.0 if lev else 20.0,
        )
        c.score = _score(c)
        cands.append(c)

    cands.sort(key=lambda x: x.score, reverse=True)
    return cands[:5]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for c in screen_futures():
        print(c.ticker, c.name, c.score, c.reasons)
