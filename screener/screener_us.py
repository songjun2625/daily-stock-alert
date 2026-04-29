"""
미장 스윙 스크리너 — 3~5일 보유, 영업이익률 높은 저평가 종목.

설계는 두 입력을 합친 결과:
  1) 사업화 리포트(2026.04.29) §3-3 스크리너 5단계
  2) us-swing-screener 스킬 스펙 — 영업이익률·매출 성장·RSI·52주 드로우다운·신호등·손절가·원화 병기

발송 메시지는 모든 회원에게 동일(1:多 일방 발송)하며, 회원별 맞춤 추천은 절대 만들지 않는다.
초보자 어조와 손절가 안내, 신호등은 메시지 텍스트 빌더(`build_kakao_message_us`)에서 출력한다.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

import pandas as pd
import yfinance as yf

from concurrent.futures import ThreadPoolExecutor, as_completed
from . import indicators as ind
from . import data_sources as ds
from .config import US_THRESH, USD_KRW_FALLBACK, TOP_N_US

log = logging.getLogger(__name__)

# 1차 유니버스 — 시총 큰 미국주식 + 사용자가 선호하는 고마진 성장주.
# 운영 시에는 Finnhub/Polygon 일별 스냅샷에서 영업이익률·매출성장 필터로 자동 추출.
DEFAULT_UNIVERSE = [
    # 메가캡 (벤치마크용)
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    # 사용자 선호: 고영업이익률 성장주
    "APP",   # 앱러빈
    "GE",    # GE Aerospace
    "NOW", "CRWD", "SNOW", "PANW", "ADBE", "AVGO", "ASML",
    "PLTR", "COIN", "SHOP", "AMD", "NFLX",
    # 레버리지 ETF (포지션 사이징 안내용 — 추천 자체엔 사용 안 함)
]


@dataclass
class USCandidate:
    ticker: str
    name: str
    price: float
    price_krw: float
    change_pct_1d: float
    market_cap: float
    operating_margin: Optional[float]
    revenue_growth: Optional[float]
    pe_ratio: Optional[float]
    sector: Optional[str]
    avg_volume: float
    rsi: float
    drawdown_52w: float                 # 52주 고점 대비 -X (양수)
    macd_golden_cross: bool
    ma_aligned_up: bool
    volume_spike: bool
    earnings_surprise_pct: Optional[float] = None  # 최근 분기 EPS surprise(%)
    score: float = 0.0
    reasons: list = field(default_factory=list)
    # 매매 가이드 (us-swing-screener 스킬 스펙 출력 형식)
    entry_low: float = 0.0
    entry_high: float = 0.0
    target: float = 0.0
    stoploss: float = 0.0


def _fetch_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """stooq → yfinance 폴백. Azure IP 차단 우회용."""
    df = ds.fetch_history(ticker, market="us", period_days=365 if period == "1y" else 730)
    return df if df is not None else pd.DataFrame()


def _fetch_fundamentals(ticker: str) -> dict:
    """yfinance fast_info + info 조합. 운영 시 Finnhub로 교체 권장."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "name": info.get("shortName") or info.get("longName") or ticker,
            "market_cap": float(info.get("marketCap") or 0),
            "operating_margin": float(info.get("operatingMargins") or 0) or None,
            "revenue_growth": float(info.get("revenueGrowth") or 0) or None,
            "pe_ratio": float(info.get("trailingPE") or 0) or None,
            "sector": info.get("sector"),
        }
    except Exception as e:
        log.warning("fundamentals fetch failed for %s: %s", ticker, e)
        return {"name": ticker, "market_cap": 0, "operating_margin": None,
                "revenue_growth": None, "pe_ratio": None, "sector": None}


def _score(c: USCandidate) -> float:
    """가중 점수 — 좋은 회사(영업이익률·성장)에 가중치를 더 둔다."""
    s = 0.0
    if c.operating_margin and c.operating_margin >= US_THRESH.operating_margin_min:
        s += 25 * min(c.operating_margin / US_THRESH.operating_margin_min, 2.5)
    if c.revenue_growth and c.revenue_growth >= US_THRESH.revenue_growth_min:
        s += 15 * min(c.revenue_growth / US_THRESH.revenue_growth_min, 2.5)
    if US_THRESH.rsi_low <= c.rsi <= US_THRESH.rsi_high:
        s += 20
    if US_THRESH.drawdown_low <= c.drawdown_52w <= US_THRESH.drawdown_high:
        s += 15
    if c.macd_golden_cross:
        s += 10
    if c.ma_aligned_up:
        s += 8
    if c.volume_spike:
        s += 7
    if c.earnings_surprise_pct and c.earnings_surprise_pct >= 5:
        s += 12  # us-swing-screener 스킬 스펙: 어닝 서프라이즈 +5% 이상
    return round(s, 2)


def _trade_plan(price: float, drawdown: float, atr: Optional[float] = None) -> tuple[float, float, float, float]:
    """진입 구간·목표가·손절가.
    ATR 가능하면 ATR 기반(더 견고), 없으면 % 기반.
      - 진입 구간: 현재가 ± 0.7×ATR (또는 ±1.5%)
      - 손절: 현재가 - 1.5×ATR (또는 -4%)
      - 목표: 손절폭의 1.8배 (RR 1:1.8)
    """
    if atr and atr > 0:
        entry_low  = round(price - 0.7 * atr, 2)
        entry_high = round(price + 0.5 * atr, 2)
        stoploss   = round(price - 1.5 * atr, 2)
        risk       = price - stoploss
        target     = round(price + 1.8 * risk, 2)
    else:
        entry_low  = round(price * 0.985, 2)
        entry_high = round(price * 1.005, 2)
        stoploss   = round(price * 0.96, 2)
        target     = round(price * (1 + max(0.04, drawdown * 0.4)), 2)
    return entry_low, entry_high, target, stoploss


def _atr(hist: pd.DataFrame, period: int = 14) -> Optional[float]:
    if hist is None or len(hist) < period + 1: return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _evaluate_one(ticker: str, fx: float) -> Optional[USCandidate]:
    hist = _fetch_history(ticker)
    if hist.empty or len(hist) < 60:
        return None
    close = hist["Close"]; vol = hist["Volume"]
    rsi_v = float(ind.rsi(close).iloc[-1])
    macd_l, sig_l, _ = ind.macd(close)
    gx = ind.is_macd_golden_cross(macd_l, sig_l)
    ma_up = ind.is_ma_aligned_up(close)
    vspike = ind.volume_spike(vol)
    dd = ind.drawdown_from_52w_high(close)
    avg_vol = float(vol.tail(20).mean())
    price = float(close.iloc[-1])
    chg_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) >= 2 else 0.0
    atr = _atr(hist)

    f = _fetch_fundamentals(ticker)

    # 1단계: 좋은 회사 — 모두 통과해야 후보
    if f["market_cap"] < US_THRESH.market_cap_min: return None
    if avg_vol < US_THRESH.avg_volume_min: return None
    if f["operating_margin"] is None or f["operating_margin"] < US_THRESH.operating_margin_min: return None
    if f["revenue_growth"] is None or f["revenue_growth"] < US_THRESH.revenue_growth_min: return None

    cheap_signals = sum([
        US_THRESH.rsi_low <= rsi_v <= US_THRESH.rsi_high,
        US_THRESH.drawdown_low <= dd <= US_THRESH.drawdown_high,
    ])
    entry_signals = sum([gx, ma_up, vspike])
    if cheap_signals < 1 or entry_signals < 1: return None

    es = ds.us_earnings_surprise(ticker)

    reasons = []
    if US_THRESH.rsi_low <= rsi_v <= US_THRESH.rsi_high: reasons.append(f"RSI {rsi_v:.0f}(과매도 반등 후보)")
    if US_THRESH.drawdown_low <= dd <= US_THRESH.drawdown_high: reasons.append(f"52주 고점 대비 -{dd*100:.0f}%")
    if gx: reasons.append("MACD 골든크로스")
    if ma_up: reasons.append("5/20일선 정배열 시작")
    if vspike: reasons.append("거래량 5일 평균 2배+")
    reasons.append(f"영업이익률 {f['operating_margin']*100:.0f}%")
    if es and es >= 5: reasons.append(f"어닝 서프라이즈 +{es:.1f}%")

    e_lo, e_hi, tgt, stop = _trade_plan(price, dd, atr=atr)
    c = USCandidate(
        ticker=ticker, name=f["name"],
        price=price, price_krw=round(price * fx),
        change_pct_1d=chg_1d, market_cap=f["market_cap"],
        operating_margin=f["operating_margin"], revenue_growth=f["revenue_growth"],
        pe_ratio=f["pe_ratio"], sector=f["sector"],
        avg_volume=avg_vol, rsi=rsi_v, drawdown_52w=dd,
        macd_golden_cross=gx, ma_aligned_up=ma_up, volume_spike=vspike,
        earnings_surprise_pct=es,
        score=0.0, reasons=reasons,
        entry_low=e_lo, entry_high=e_hi, target=tgt, stoploss=stop,
    )
    c.score = _score(c)
    return c


def screen_us(universe: Iterable[str] = DEFAULT_UNIVERSE,
              fx_usd_krw: Optional[float] = None,
              top_n: int = TOP_N_US,
              parallel: int = 8) -> list[USCandidate]:
    """병렬 yfinance fetch + 캐싱된 환율·어닝. 30개 유니버스 ≤ 30초."""
    fx = fx_usd_krw if fx_usd_krw is not None else ds.usd_krw(fallback=USD_KRW_FALLBACK)
    cands: list[USCandidate] = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(_evaluate_one, t, fx): t for t in universe}
        for fut in as_completed(futures):
            try:
                c = fut.result()
                if c is not None:
                    cands.append(c)
            except Exception as e:
                log.warning("evaluate %s failed: %s", futures[fut], e)
    cands.sort(key=lambda x: x.score, reverse=True)
    return cands[:top_n]


# ---- VIX / 시장 신호등 -----------------------------------------------------

def market_traffic_light() -> dict:
    """오늘 미장 매수해도 되는 날인지 신호등 — 🟢🟡🔴.

    스킬 스펙에 따라 VIX 기준:
      VIX <= 20 → 🟢 매수 OK
      20 < VIX <= 25 → 🟡 조심해서 매수 (포지션 70%로 축소)
      VIX > 25 → 🔴 오늘은 쉬자
    """
    vix = ds.vix_close(fallback=18.0)
    if vix <= 20:
        light, label, msg = "🟢", "매수 OK", "공포지수 낮아서 매수 괜찮은 날"
    elif vix <= 25:
        light, label, msg = "🟡", "조심해서 매수", "변동성 다소 높음 — 평소의 70% 사이즈로"
    else:
        light, label, msg = "🔴", "오늘은 쉬자", "변동성 매우 높음 — 신규 진입 자제"
    return {"vix": round(vix, 2), "light": light, "label": label, "summary": msg}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    picks = screen_us()
    for p in picks:
        print(p.ticker, p.score, p.reasons)
    print(market_traffic_light())
