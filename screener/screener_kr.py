"""
한국장(코스피·코스닥) 스윙 스크리너 — 사업화 리포트 §3-3 그대로.

조건:
  1) 거래량 5일 평균 대비 2배 이상 + RSI 30~40 (저평가 반등)
  2) MACD 골든크로스 + 5일선·20일선 정배열 시작
  3) 어닝 서프라이즈 (컨센서스 +5% 이상) — 시즌 외에는 스킵
  4) 기관·외국인 5일 연속 순매수
  5) 점수화 → 상위 3~5개

데이터 소스: FinanceDataReader (무료) + pykrx (수급 데이터).
운영 시 키움 OpenAPI / 한국투자증권 KIS API로 실시간 보강 권장.
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

from . import indicators as ind
from . import data_sources as ds
from .config import KR_THRESH, TOP_N_KR, KST

log = logging.getLogger(__name__)

# 1차 유니버스: 코스피200 + 코스닥150 핵심 — 운영 시 시총·유동성 필터로 자동 갱신.
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
    sector: str                   # '대형주' / '중형주' / '코스닥' 등 — 미장 카드와 동일한 칩에 사용
    price: int
    change_pct_1d: float
    market_cap: float
    rsi: float
    volume_spike: bool
    macd_golden_cross: bool
    ma_aligned_up: bool
    foreign_streak: int           # 외국인 순매수 연속일
    institution_streak: int       # 기관 순매수 연속일
    earnings_surprise: Optional[float] = None  # OpenDART 키 있을 때만
    score: float = 0.0
    reasons: list = field(default_factory=list)
    # 매매 가이드 (원 단위)
    entry_low: int = 0
    entry_high: int = 0
    target: int = 0
    stoploss: int = 0


def _fetch_history(ticker: str) -> pd.DataFrame:
    """KR 시계열 — FDR 우선, 실패 시 stooq 폴백 (KOSPI/KOSDAQ ticker .KS suffix)."""
    if fdr is not None:
        try:
            end = datetime.now(KST).date()
            start = end - timedelta(days=200)
            df = fdr.DataReader(ticker, start, end)
            if df is not None and not df.empty:
                df.columns = [c.capitalize() for c in df.columns]
                return df
        except Exception as e:
            log.warning("FDR fetch failed for %s: %s — stooq 폴백 시도", ticker, e)
    df = ds.fetch_history(ticker, market="kr", period_days=200)
    return df if df is not None else pd.DataFrame()


# KR 종목명 하드코딩 맵 — FDR StockListing이 GH Actions에서 실패 시 fallback.
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


# 시총 기준 단순 분류 — 화면 칩에 사용. 운영 시 KRX 분류 코드로 정밀화 가능.
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


def _atr(hist: pd.DataFrame, period: int = 14) -> Optional[float]:
    if hist is None or len(hist) < period + 1: return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _kr_trade_plan(price: int, atr: Optional[float]) -> tuple[int,int,int,int]:
    """KR 매매 가이드 — ATR 가능 시 ATR 기반, 아니면 % 기반.
    한국장은 호가 단위가 있어 단순 정수로 반올림 (실호가 단위는 운영 시 종목별 적용)."""
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
        stop       = int(round(p * 0.96))
        target     = int(round(p * 1.06))
    return entry_low, entry_high, target, stop


def _name_of(ticker: str) -> str:
    if ticker in KR_TICKER_NAMES:
        return KR_TICKER_NAMES[ticker]
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
    """기관·외국인 순매수 연속일수 (pykrx 일별 거래대금 → 양수 연속). 캐싱 6시간."""
    res = ds.kr_supply_streak(ticker, days=days)
    return int(res.get("foreign", 0)), int(res.get("institution", 0))


def _score(c: KRCandidate) -> float:
    s = 0.0
    if c.volume_spike: s += 25
    if KR_THRESH.rsi_low <= c.rsi <= KR_THRESH.rsi_high: s += 25
    if c.macd_golden_cross: s += 15
    if c.ma_aligned_up: s += 10
    if c.foreign_streak >= KR_THRESH.institutional_streak_days: s += 12
    if c.institution_streak >= KR_THRESH.institutional_streak_days: s += 13
    if c.earnings_surprise and c.earnings_surprise >= KR_THRESH.earnings_surprise_min * 100:
        s += 10
    return round(s, 2)


def screen_kr(universe: Iterable[str] = DEFAULT_KR_UNIVERSE,
              top_n: int = TOP_N_KR) -> list[KRCandidate]:
    cands: list[KRCandidate] = []
    fallback: list[KRCandidate] = []   # 게이트 미통과 종목도 점수로 정렬해 백업으로 보유
    for ticker in universe:
        hist = _fetch_history(ticker)
        if hist.empty or len(hist) < 60:
            continue
        close = hist["Close"]
        vol = hist["Volume"]

        rsi_v = float(ind.rsi(close).iloc[-1])
        macd_l, sig_l, _ = ind.macd(close)
        gx = ind.is_macd_golden_cross(macd_l, sig_l)
        ma_up = ind.is_ma_aligned_up(close)
        vspike = ind.volume_spike(vol, multiplier=KR_THRESH.volume_multiplier_min)

        # KR 게이트 (AND 조건 — 옵션 A): 거짓 양성 제거를 위해 모든 진입 신호가 동시 충족돼야 함.
        # 1차 조건 (필수, AND): RSI 30~40 + 거래량 5일평균 2배+
        # 2차 조건 (보조 가점): MACD 골든크로스 또는 5/20일선 정배열 시작
        rsi_ok = (KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high)
        primary_pass = bool(vspike and rsi_ok)
        secondary = bool(gx or ma_up)
        gate_passed = primary_pass  # 1차 통과해야만 정식 진입 후보. 2차는 점수 가산.

        mc = _market_cap(ticker)
        if mc and mc < KR_THRESH.market_cap_min_krw:
            continue

        f_streak, i_streak = _institution_foreign_streak(ticker)
        es = ds.kr_earnings_surprise(ticker)

        reasons = []
        if vspike: reasons.append("거래량 5일 평균 2배+")
        if KR_THRESH.rsi_low <= rsi_v <= KR_THRESH.rsi_high:
            reasons.append(f"RSI {rsi_v:.0f}(저평가 반등 후보)")
        if gx: reasons.append("MACD 골든크로스")
        if ma_up: reasons.append("5/20일선 정배열 시작")
        if f_streak >= 5: reasons.append(f"외국인 {f_streak}일 연속 순매수")
        if i_streak >= 5: reasons.append(f"기관 {i_streak}일 연속 순매수")
        if es and es >= KR_THRESH.earnings_surprise_min * 100:
            reasons.append(f"어닝 서프라이즈 +{es:.1f}%")

        price = int(close.iloc[-1])
        chg_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) >= 2 else 0.0
        atr = _atr(hist)
        e_lo, e_hi, tgt, stop = _kr_trade_plan(price, atr)
        sector = KR_SECTOR_OVERRIDE.get(ticker, "코스피·코스닥")

        c = KRCandidate(
            ticker=ticker, name=_name_of(ticker), sector=sector,
            price=price, change_pct_1d=chg_1d, market_cap=mc,
            rsi=rsi_v, volume_spike=vspike,
            macd_golden_cross=gx, ma_aligned_up=ma_up,
            foreign_streak=f_streak, institution_streak=i_streak,
            earnings_surprise=es,
            score=0.0, reasons=reasons,
            entry_low=e_lo, entry_high=e_hi, target=tgt, stoploss=stop,
        )
        c.score = _score(c)
        if gate_passed:
            cands.append(c)
        else:
            # 게이트 미통과 — 부분 점수만 부여 (강세 추세지만 진입 신호는 아직 약함)
            if c.score == 0:
                # 최소한 점수 매겨두기 — 나중에 fallback으로 사용
                c.score = max(0, 50 - abs(rsi_v - 50))  # RSI 50 기준 거리 역수
                c.reasons.append("진입 신호 미발생 — 추세 강세 종목")
            fallback.append(c)

    cands.sort(key=lambda x: x.score, reverse=True)
    fallback.sort(key=lambda x: x.score, reverse=True)
    # 게이트 통과 종목이 부족하면 fallback으로 채움
    if len(cands) < top_n:
        cands.extend(fallback[: top_n - len(cands)])
    return cands[:top_n]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for p in screen_kr():
        print(p.ticker, p.name, p.score, p.reasons)
