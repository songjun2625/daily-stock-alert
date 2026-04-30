"""
한국장(코스피·코스닥) 스윙 스크리너 — US 알고리즘(2026.04 백테스트 +54%) 이식 버전.

설계 원칙 (지정학 요소 배제 — US 와 동일한 펀더멘털·기술 신호만):
  1) 좋은 회사 게이트 — 영업이익률 ≥ 10%, 매출 성장 ≥ 5% (KR 기업 평균 고려해 US 의 20%/10% 보다 완화)
  2) 저평가 신호 — RSI 30~45 또는 52주 고점 대비 -10~-35% 드로우다운
  3) 진입 신호 — MACD 골든크로스 / 5·20일선 정배열 / 거래량 5일평균 1.5배+
  4) 게이트(완화 OR): cheap_signals + entry_signals ≥ 1 (둘 다 0이면 탈락)
  5) 가점 — 외국인·기관 5일 연속 순매수, 어닝 서프라이즈 +5%
  6) 점수화 → 상위 3~5개

데이터 소스: yfinance(.KS/.KQ suffix → fundamentals + 시계열) + FinanceDataReader 폴백 + pykrx(수급).
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

import pandas as pd

try:
    import FinanceDataReader as fdr
except Exception:
    fdr = None

try:
    from pykrx import stock as krx
except Exception:
    krx = None

try:
    import yfinance as yf
except Exception:
    yf = None

from concurrent.futures import ThreadPoolExecutor, as_completed
from . import indicators as ind
from . import data_sources as ds
from .config import KR_THRESH, TOP_N_KR, KST

log = logging.getLogger(__name__)

# 1차 유니버스: 코스피·코스닥 핵심 — 운영 시 시총·유동성 필터로 자동 갱신.
DEFAULT_KR_UNIVERSE = [
    # 코스피 대형주
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "051910",  # LG화학
    "005380",  # 현대차
    "035420",  # NAVER
    "035720",  # 카카오
    "207940",  # 삼성바이오로직스
    "068270",  # 셀트리온
    "373220",  # LG에너지솔루션
    "012450",  # 한화에어로스페이스
    "000270",  # 기아
    "105560",  # KB금융
    "055550",  # 신한지주
    "017670",  # SK텔레콤
    "015760",  # 한국전력
    "009150",  # 삼성전기
    "006400",  # 삼성SDI
    "032830",  # 삼성생명
    "066570",  # LG전자
    "003670",  # 포스코홀딩스
    # 코스피 중형주 / 코스닥
    "042700",  # 한미반도체
    "247540",  # 에코프로비엠
    "086520",  # 에코프로
    "028300",  # HLB
    "066970",  # 엘앤에프
    "196170",  # 알테오젠
    "091990",  # 셀트리온헬스케어
    "263750",  # 펄어비스
    "041510",  # SM
    "352820",  # 하이브
]


@dataclass
class KRCandidate:
    ticker: str
    name: str
    sector: str
    price: int
    change_pct_1d: float
    market_cap: float
    operating_margin: Optional[float]   # 영업이익률 (US 알고리즘 이식)
    revenue_growth: Optional[float]     # 매출 성장률
    pe_ratio: Optional[float]
    drawdown_52w: float                 # 52주 고점 대비 하락 (양수)
    rsi: float
    volume_spike: bool
    macd_golden_cross: bool
    ma_aligned_up: bool
    foreign_streak: int
    institution_streak: int
    earnings_surprise: Optional[float] = None
    score: float = 0.0
    reasons: list = field(default_factory=list)
    entry_low: int = 0
    entry_high: int = 0
    target: int = 0
    stoploss: int = 0


# KR 종목명 하드코딩 맵 — FDR StockListing 실패 시 fallback.
KR_TICKER_NAMES: dict[str, str] = {
    "005930": "삼성전자",      "000660": "SK하이닉스",      "051910": "LG화학",
    "005380": "현대차",        "035420": "NAVER",          "035720": "카카오",
    "207940": "삼성바이오로직스", "068270": "셀트리온",       "042700": "한미반도체",
    "373220": "LG에너지솔루션",  "247540": "에코프로비엠",   "086520": "에코프로",
    "028300": "HLB",           "066970": "엘앤에프",        "012450": "한화에어로스페이스",
    "000270": "기아",          "105560": "KB금융",         "055550": "신한지주",
    "017670": "SK텔레콤",       "015760": "한국전력",       "009150": "삼성전기",
    "006400": "삼성SDI",       "032830": "삼성생명",        "066570": "LG전자",
    "003670": "포스코홀딩스",   "196170": "알테오젠",        "091990": "셀트리온헬스케어",
    "263750": "펄어비스",       "041510": "SM",             "352820": "하이브",
}


KR_SECTOR_OVERRIDE: dict[str, str] = {
    "005930": "반도체",          "000660": "반도체",        "042700": "반도체",
    "051910": "화학·배터리",      "373220": "화학·배터리",   "247540": "화학·배터리",
    "086520": "화학·배터리",      "066970": "화학·배터리",   "006400": "화학·배터리",
    "005380": "자동차",          "000270": "자동차",
    "035420": "IT 플랫폼",       "035720": "IT 플랫폼",      "066570": "IT 가전",
    "207940": "바이오·헬스케어",   "068270": "바이오·헬스케어", "028300": "바이오·헬스케어",
    "196170": "바이오·헬스케어",   "091990": "바이오·헬스케어",
    "012450": "방산",            "003670": "철강·소재",
    "105560": "금융",            "055550": "금융",          "032830": "금융",
    "017670": "통신",            "015760": "유틸리티",
    "009150": "전자부품",
    "263750": "게임",            "041510": "엔터",          "352820": "엔터",
}


def _fetch_history(ticker: str) -> pd.DataFrame:
    """KR 시계열 — yfinance(.KS/.KQ) 우선, FDR/stooq 폴백."""
    df = ds.fetch_history(ticker, market="kr", period_days=400)
    if df is not None and not df.empty:
        return df
    if fdr is not None:
        try:
            end = datetime.now(KST).date()
            start = end - timedelta(days=400)
            df = fdr.DataReader(ticker, start, end)
            if df is not None and not df.empty:
                df.columns = [c.capitalize() for c in df.columns]
                return df
        except Exception as e:
            log.warning("FDR fetch failed for %s: %s", ticker, e)
    return pd.DataFrame()


def _fetch_kr_fundamentals(ticker: str) -> dict:
    """yfinance .KS / .KQ — 영업이익률, 매출 성장률, PER, 시총.
    실패 시 {operating_margin: None, ...} 반환 → 펀더멘털 미공시 종목도 통과(점수만 낮음)."""
    if yf is None:
        return {"name": KR_TICKER_NAMES.get(ticker, ticker), "market_cap": 0,
                "operating_margin": None, "revenue_growth": None, "pe_ratio": None}
    for suffix in (".KS", ".KQ"):
        try:
            t = yf.Ticker(ticker + suffix)
            info = t.info or {}
            if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                continue
            return {
                "name": info.get("shortName") or info.get("longName") or KR_TICKER_NAMES.get(ticker, ticker),
                "market_cap": float(info.get("marketCap") or 0),
                "operating_margin": float(info.get("operatingMargins") or 0) or None,
                "revenue_growth": float(info.get("revenueGrowth") or 0) or None,
                "pe_ratio": float(info.get("trailingPE") or 0) or None,
            }
        except Exception as e:
            log.debug("yfinance %s%s fundamentals failed: %s", ticker, suffix, e)
            continue
    return {"name": KR_TICKER_NAMES.get(ticker, ticker), "market_cap": 0,
            "operating_margin": None, "revenue_growth": None, "pe_ratio": None}


def _atr(hist: pd.DataFrame, period: int = 14) -> Optional[float]:
    if hist is None or len(hist) < period + 1: return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _kr_trade_plan(price: int, drawdown: float, atr: Optional[float]) -> tuple[int,int,int,int]:
    """KR 매매 가이드 — US 와 동일한 RR 1:1.8 구조.
      - ATR 가능: 진입 ±0.7~0.5×ATR / 손절 -1.5×ATR / 목표 손절폭 ×1.8
      - 폴백: ±1.5% / -2.5% / +4% (KR 변동폭 좁아 US 보다 타이트)."""
    p = float(price)
    if atr and atr > 0:
        entry_low  = int(round(p - 0.7 * atr))
        entry_high = int(round(p + 0.5 * atr))
        stop       = int(round(p - 1.5 * atr))
        risk       = p - stop
        target     = int(round(p + 1.8 * risk))
    else:
        entry_low  = int(round(p * 0.985))
        entry_high = int(round(p * 1.005))
        stop       = int(round(p * 0.975))
        target     = int(round(p * (1 + max(0.04, drawdown * 0.4))))
    return entry_low, entry_high, target, stop


def _name_of(ticker: str, fallback: str | None = None) -> str:
    if ticker in KR_TICKER_NAMES:
        return KR_TICKER_NAMES[ticker]
    if fallback:
        return fallback
    if fdr is None:
        return ticker
    try:
        listing = fdr.StockListing("KRX")
        row = listing[listing["Code"] == ticker]
        if not row.empty:
            return row["Name"].iloc[0]
    except Exception:
        pass
    return ticker


def _market_cap(ticker: str) -> float:
    if krx is None:
        return 0.0
    try:
        d = datetime.now(KST).strftime("%Y%m%d")
        df = krx.get_market_cap_by_date(d, d, ticker)
        if df is None or df.empty:
            return 0.0
        return float(df["시가총액"].iloc[-1])
    except Exception:
        return 0.0


def _institution_foreign_streak(ticker: str, days: int = 7) -> tuple[int, int]:
    """기관·외국인 순매수 연속일수 (pykrx). 캐싱 6시간."""
    res = ds.kr_supply_streak(ticker, days=days)
    return int(res.get("foreign", 0)), int(res.get("institution", 0))


def _score(c: KRCandidate) -> float:
    """US 와 동일한 가중 — 좋은 회사(영업이익률·성장)에 가중치 최대.

    임계값을 통과한 만큼 비례 보너스 (cap 2.5×):
      - 영업이익률 ≥10% : 25 ~ 62.5점 (실제 수치/임계값 비례)
      - 매출 성장 ≥5%   : 15 ~ 37.5점
      - RSI 30~45        : +20점
      - 드로우다운 -10~-35%: +15점
      - MACD 골든크로스   : +10점
      - 5/20일선 정배열   : +8점
      - 거래량 1.5배+    : +7점
      - 어닝 서프라이즈 ≥5%: +12점
      - (KR 전용 가점) 외국인 5일+ : +12 / 기관 5일+ : +13
    """
    s = 0.0
    if c.operating_margin and c.operating_margin >= KR_THRESH.operating_margin_min:
        s += 25 * min(c.operating_margin / KR_THRESH.operating_margin_min, 2.5)
    if c.revenue_growth and c.revenue_growth >= KR_THRESH.revenue_growth_min:
        s += 15 * min(c.revenue_growth / KR_THRESH.revenue_growth_min, 2.5)
    if KR_THRESH.rsi_low <= c.rsi <= KR_THRESH.rsi_high:
        s += 20
    if KR_THRESH.drawdown_low <= c.drawdown_52w <= KR_THRESH.drawdown_high:
        s += 15
    if c.macd_golden_cross:
        s += 10
    if c.ma_aligned_up:
        s += 8
    if c.volume_spike:
        s += 7
    if c.earnings_surprise and c.earnings_surprise >= KR_THRESH.earnings_surprise_min * 100:
        s += 12
    # KR 고유 — 수급 신호
    if c.foreign_streak >= KR_THRESH.institutional_streak_days:
        s += 12
    if c.institution_streak >= KR_THRESH.institutional_streak_days:
        s += 13
    return round(s, 2)


def _evaluate_one(ticker: str) -> Optional[KRCandidate]:
    hist = _fetch_history(ticker)
    if hist.empty or len(hist) < 60:
        return None
    close = hist["Close"]
    vol = hist["Volume"]
    rsi_v = float(ind.rsi(close).iloc[-1])
    macd_l, sig_l, _ = ind.macd(close)
    gx = ind.is_macd_golden_cross(macd_l, sig_l)
    ma_up = ind.is_ma_aligned_up(close)
    vspike = ind.volume_spike(vol, multiplier=KR_THRESH.volume_multiplier_min)
    dd = ind.drawdown_from_52w_high(close)
    price = int(close.iloc[-1])
    chg_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) >= 2 else 0.0
    atr = _atr(hist)

    f = _fetch_kr_fundamentals(ticker)
    mc = f.get("market_cap") or _market_cap(ticker)

    # 시총 필터 — 5천억 이상 (펀더멘털 데이터 안정성 확보)
    if mc and mc < KR_THRESH.market_cap_min_krw:
        return None
    # 영업이익률·매출성장 데이터 있으면 임계값 검증, None 이면 통과(점수만 낮음)
    if f["operating_margin"] is not None and f["operating_margin"] < KR_THRESH.operating_margin_min:
        return None
    if f["revenue_growth"] is not None and f["revenue_growth"] < KR_THRESH.revenue_growth_min:
        return None

    # 게이트: cheap (RSI / 드로우다운) + entry (gx / ma_up / vspike) 합쳐 1개 이상
    cheap_signals = sum([
        KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high,
        KR_THRESH.drawdown_low <= dd <= KR_THRESH.drawdown_high,
    ])
    entry_signals = sum([gx, ma_up, vspike])
    if cheap_signals + entry_signals < 1:
        return None

    # 수급(외국인·기관) — 캐싱되어 있어 부담 적음
    f_streak, i_streak = _institution_foreign_streak(ticker)
    es = ds.kr_earnings_surprise(ticker)

    reasons = []
    if KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high:
        reasons.append(f"RSI {rsi_v:.0f}(과매도 반등 후보)")
    if KR_THRESH.drawdown_low <= dd <= KR_THRESH.drawdown_high:
        reasons.append(f"52주 고점 대비 -{dd*100:.0f}%")
    if gx: reasons.append("MACD 골든크로스")
    if ma_up: reasons.append("5/20일선 정배열 시작")
    if vspike: reasons.append("거래량 5일평균 1.5배+")
    if f["operating_margin"]:
        reasons.append(f"영업이익률 {f['operating_margin']*100:.0f}%")
    if f["revenue_growth"] and f["revenue_growth"] >= KR_THRESH.revenue_growth_min:
        reasons.append(f"매출 성장 +{f['revenue_growth']*100:.0f}%")
    if f_streak >= KR_THRESH.institutional_streak_days:
        reasons.append(f"외국인 {f_streak}일 연속 순매수")
    if i_streak >= KR_THRESH.institutional_streak_days:
        reasons.append(f"기관 {i_streak}일 연속 순매수")
    if es and es >= KR_THRESH.earnings_surprise_min * 100:
        reasons.append(f"어닝 서프라이즈 +{es:.1f}%")

    e_lo, e_hi, tgt, stop = _kr_trade_plan(price, dd, atr)
    sector = KR_SECTOR_OVERRIDE.get(ticker, "코스피·코스닥")

    c = KRCandidate(
        ticker=ticker, name=_name_of(ticker, f.get("name")), sector=sector,
        price=price, change_pct_1d=chg_1d, market_cap=mc or 0.0,
        operating_margin=f["operating_margin"], revenue_growth=f["revenue_growth"],
        pe_ratio=f["pe_ratio"], drawdown_52w=dd,
        rsi=rsi_v, volume_spike=vspike,
        macd_golden_cross=gx, ma_aligned_up=ma_up,
        foreign_streak=f_streak, institution_streak=i_streak,
        earnings_surprise=es,
        score=0.0, reasons=reasons,
        entry_low=e_lo, entry_high=e_hi, target=tgt, stoploss=stop,
    )
    c.score = _score(c)
    return c


def screen_kr(universe: Iterable[str] = DEFAULT_KR_UNIVERSE,
              top_n: int = TOP_N_KR,
              parallel: int = 6) -> list[KRCandidate]:
    """병렬 fetch — 30개 유니버스 ≤ 60초 (yfinance fundamentals 호출 지연 고려)."""
    cands: list[KRCandidate] = []
    universe_list = list(universe)
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futures = {ex.submit(_evaluate_one, t): t for t in universe_list}
        for fut in as_completed(futures):
            try:
                c = fut.result()
                if c is not None:
                    cands.append(c)
            except Exception as e:
                log.warning("evaluate %s failed: %s", futures[fut], e)
    cands.sort(key=lambda x: x.score, reverse=True)
    return cands[:top_n]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for p in screen_kr():
        print(p.ticker, p.name, p.score, p.reasons)
