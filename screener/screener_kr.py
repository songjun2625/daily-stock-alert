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
from .config import KR_THRESH, KR_SECTOR_BONUS, TOP_N_KR, KST

log = logging.getLogger(__name__)

# 확장 유니버스 — 2026 핫섹터 (반도체·HBM·2차전지·방산·전력) 위주.
# 1월~4월 승자 TOP 15 분석에서 추출 (analyze_kr_winners.py).
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
    "012330",  # 현대모비스
    # 반도체 사이클 (HBM·AI 메모리)
    "042700",  # 한미반도체 (HBM)
    "388050",  # SFA반도체
    "058470",  # 리노공업
    "240810",  # 원익IPS
    "036930",  # 주성엔지니어링 (분석 1위 +313%)
    "095340",  # ISC
    "403870",  # HPSP
    "140860",  # 파크시스템스
    "108320",  # LX세미콘
    # 2차전지·소재
    "247540",  # 에코프로비엠
    "086520",  # 에코프로
    "066970",  # 엘앤에프
    "020150",  # 일진머티리얼즈
    "002990",  # 금호석유
    "010120",  # LS ELECTRIC (전력기기)
    # 바이오·헬스케어 (분석상 약세 — 후순위)
    "028300", "196170", "302440", "214450", "145020",
    # 엔터·게임
    "035900", "041510", "352820", "263750",
    "112040", "036570", "251270",
    # 방산·우주
    "079550",  # LIG넥스원
    "047810",  # 한국항공우주 (KAI)
    "272210",  # 한화시스템
    # 조선
    "010140",  # 삼성중공업
    "009540",  # HD한국조선해양
    "042660",  # 한화오션
    # 전선·전력 인프라 (AI 데이터센터·송배전 테마) — 2026 핫섹터, 거래대금 TOP10
    "001440",  # 대한전선
    "010170",  # 대한광통신
    "006910",  # 보성파워텍
    "006340",  # 대원전선
    "062040",  # 산일전기
    "000500",  # 가온전선
    "103590",  # 일진전기
    "298040",  # 효성중공업 (변압기·전력기기)
    "267260",  # HD현대일렉트릭 (변압기·송배전)
    "112610",  # CS윈드 (풍력·전력 인프라)
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
# 확장 유니버스(반도체·2차전지·바이오·게임·방산·조선·자동차부품)까지 포함.
KR_TICKER_NAMES: dict[str, str] = {
    # 코스피 대형주
    "005930": "삼성전자",      "000660": "SK하이닉스",      "051910": "LG화학",
    "005380": "현대차",        "035420": "NAVER",          "035720": "카카오",
    "207940": "삼성바이오로직스", "068270": "셀트리온",
    "373220": "LG에너지솔루션",  "012450": "한화에어로스페이스",
    "000270": "기아",          "105560": "KB금융",         "055550": "신한지주",
    "017670": "SK텔레콤",       "015760": "한국전력",       "009150": "삼성전기",
    "006400": "삼성SDI",       "032830": "삼성생명",        "066570": "LG전자",
    "003670": "포스코홀딩스",   "012330": "현대모비스",
    # 반도체 사이클 (HBM·AI·장비·검사·소재)
    "042700": "한미반도체",     "388050": "SFA반도체",      "058470": "리노공업",
    "240810": "원익IPS",       "036930": "주성엔지니어링",   "095340": "ISC",
    "403870": "HPSP",          "140860": "파크시스템스",    "108320": "LX세미콘",
    # 2차전지·소재
    "247540": "에코프로비엠",   "086520": "에코프로",        "066970": "엘앤에프",
    "020150": "일진머티리얼즈",  "002990": "금호석유",        "010120": "LS ELECTRIC",
    # 바이오·헬스케어
    "028300": "HLB",           "196170": "알테오젠",        "091990": "셀트리온헬스케어",
    "064550": "바이오니아",      "302440": "SK바이오사이언스", "214450": "파마리서치",
    "145020": "휴젤",
    # 엔터·게임
    "035900": "JYP Ent.",      "041510": "SM",             "352820": "하이브",
    "263750": "펄어비스",       "112040": "위메이드",        "036570": "엔씨소프트",
    "251270": "넷마블",
    # 방산·우주
    "079550": "LIG넥스원",      "047810": "한국항공우주",     "272210": "한화시스템",
    # 조선·해운
    "010140": "삼성중공업",      "009540": "HD한국조선해양",   "042660": "한화오션",
    # 자동차 부품
    "018880": "한온시스템",
    # 전선·전력 인프라 (AI 데이터센터·송배전)
    "001440": "대한전선",        "010170": "대한광통신",      "006910": "보성파워텍",
    "006340": "대원전선",        "062040": "산일전기",        "000500": "가온전선",
    "103590": "일진전기",        "298040": "효성중공업",      "267260": "HD현대일렉트릭",
    "112610": "CS윈드",
    # 기타
    "108860": "셀바스AI",       "090430": "아모레퍼시픽",    "180640": "한진칼",
    # ETF (선물·ETF 섹션 등장 가능성)
    "069500": "KODEX 200",      "229200": "KODEX 코스닥150", "252670": "KODEX 200선물인버스2X",
    "133690": "TIGER 미국나스닥100",  "381180": "TIGER 미국필라델피아반도체나스닥",
    "122630": "KODEX 레버리지",
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
    # 전선·전력 인프라 (AI 데이터센터·송배전 테마)
    "001440": "전선·전력인프라", "010170": "전선·전력인프라", "006910": "전선·전력인프라",
    "006340": "전선·전력인프라", "062040": "전선·전력인프라", "000500": "전선·전력인프라",
    "103590": "전선·전력인프라", "298040": "전선·전력인프라", "267260": "전선·전력인프라",
    "112610": "전선·전력인프라",
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
    """KR 매매 가이드 — 백테스트 스윕 최적안 -3.5%/+6%/7d 반영.
      - ATR 가능: 진입 ±0.7~0.5×ATR / 손절 -1.5×ATR / 목표 손절폭 ×1.8
      - 폴백: ±1.5% / -3.5% / +6% (스윕 결과 KR 도 US 동일한 폭이 더 효과적)."""
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
        stop       = int(round(p * 0.965))   # -3.5%
        target     = int(round(p * 1.06))    # +6%
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
    """KR v2 알고리즘 — 2026 승자 분석 기반 (반도체·2차전지·방산 사이클 가중).

    핵심 변경:
      - 섹터 보너스 (+5~+30) — 반도체/2차전지/방산이 승자의 73%
      - RSI 골든존 30~35 (+30점) vs 일반 35~45 (+15)
      - 추세 지속 패턴 50~65 + 정배열 (+25점)
      - 깊은 드로우다운 30~45% (+25점) — 승자 평균 -34.5% 에서 반등
      - 신고가 breakout (drawdown ≤ 5% + 정배열) (+20점)
    """
    s = 0.0

    # --- 섹터 사이클 보너스 (2026 핫섹터) ---
    s += KR_SECTOR_BONUS.get(c.sector, 0)

    # --- 펀더멘털 (cap 2.5x) ---
    if c.operating_margin and c.operating_margin >= KR_THRESH.operating_margin_min:
        s += 20 * min(c.operating_margin / KR_THRESH.operating_margin_min, 2.5)
    if c.revenue_growth and c.revenue_growth >= KR_THRESH.revenue_growth_min:
        s += 12 * min(c.revenue_growth / KR_THRESH.revenue_growth_min, 2.5)

    # --- RSI 영역별 가산 (mutually exclusive) ---
    if KR_THRESH.rsi_golden_low <= c.rsi <= KR_THRESH.rsi_golden_high:
        s += 30   # 승자 평균 33.9 — 골든존
    elif KR_THRESH.rsi_golden_high < c.rsi <= KR_THRESH.rsi_high:
        s += 15   # RSI 35~45 보통 저평가
    elif KR_THRESH.rsi_trend_low <= c.rsi <= KR_THRESH.rsi_trend_high and c.ma_aligned_up:
        s += 25   # 추세 지속 (50~65 + 정배열) — 모멘텀 종목

    # --- 드로우다운 영역별 ---
    if KR_THRESH.drawdown_deep_low <= c.drawdown_52w <= KR_THRESH.drawdown_deep_high:
        s += 25   # 깊은 세일 (-30~-45%) — 승자 패턴
    elif KR_THRESH.drawdown_low <= c.drawdown_52w < KR_THRESH.drawdown_deep_low:
        s += 12   # 보통 세일 (-10~-30%)
    elif c.drawdown_52w <= KR_THRESH.drawdown_breakout_max and c.ma_aligned_up:
        s += 20   # 신고가 breakout (52w -5% 이내 + 정배열)

    # --- 진입 신호 ---
    if c.macd_golden_cross: s += 12
    if c.ma_aligned_up:     s += 10
    if c.volume_spike:      s += 10  # 1.5배+ (KR 변동성 고려 완화)

    # --- 어닝 서프라이즈 ---
    if c.earnings_surprise and c.earnings_surprise >= KR_THRESH.earnings_surprise_min * 100:
        s += 12

    # --- 수급 (외국인·기관) — 3일로 완화, 동반매수 보너스 ---
    streak = KR_THRESH.institutional_streak_days
    if c.foreign_streak >= streak:    s += 10
    if c.institution_streak >= streak: s += 10
    if c.foreign_streak >= 5 and c.institution_streak >= 5:
        s += 12   # 외+기 동반 5일+ 강한 수급

    # --- 모멘텀 폭증 가산점 (당일 +5%↑ = 시장 관심 폭증 시그널) ---
    # 거래대금 TOP 10 폭등주가 universe 에 들어왔는데 '추세 추종 못 함' 이슈 보정.
    chg = c.change_pct_1d or 0
    if chg >= 15:    s += 22   # 폭발적 (+15%↑) — 테마 모멘텀 강력
    elif chg >= 10:  s += 15   # 강한 상승 (+10~15%)
    elif chg >= 5:   s += 8    # 의미 있는 상승 (+5~10%)

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

    # 게이트 v2: cheap (RSI 30~45 / 드로우다운 ≥10% / 추세지속 / breakout) + entry 합쳐 ≥1
    rsi_cheap = KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high
    rsi_trend = (KR_THRESH.rsi_trend_low <= rsi_v <= KR_THRESH.rsi_trend_high) and ma_up
    dd_cheap = dd >= KR_THRESH.drawdown_low
    breakout = (dd <= KR_THRESH.drawdown_breakout_max) and ma_up
    cheap_signals = sum([rsi_cheap, rsi_trend, dd_cheap, breakout])
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
