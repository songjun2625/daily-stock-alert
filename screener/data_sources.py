"""
외부 데이터 소스 어댑터 — 캐싱 + 에러 격리.

소스:
  - 환율: Yahoo Finance ^KRW=X (무료, 일별)
  - KR 수급: pykrx (외국인·기관 일별 순매수)
  - KR 어닝: OpenDART API (분기 보고서) — 키 없으면 스킵
  - US 어닝: Finnhub (무료 티어 — earnings surprise) — 키 없으면 yfinance 폴백
  - VIX: Yahoo Finance ^VIX

캐싱: 같은 영업일 안에서 동일 호출은 SQLite로 재사용 → API 호출 비용·시간 절감.
"""
from __future__ import annotations
import os, json, time, sqlite3, logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

CACHE_PATH = Path(os.getenv("CACHE_DB", ".cache/data.sqlite"))
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---- SQLite 캐시 ---------------------------------------------------------

def _conn():
    c = sqlite3.connect(str(CACHE_PATH))
    c.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts INTEGER)")
    return c


def _cache_get(key: str, max_age_sec: int = 6 * 3600) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT v, ts FROM cache WHERE k=?", (key,)).fetchone()
    if not row: return None
    v, ts = row
    if time.time() - ts > max_age_sec: return None
    return v


def _cache_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO cache(k,v,ts) VALUES(?,?,?)",
                  (key, value, int(time.time())))


# ---- 환율 ---------------------------------------------------------------

def usd_krw(fallback: float = 1480.0) -> float:
    cached = _cache_get("fx:usdkrw", max_age_sec=6 * 3600)
    if cached:
        try: return float(cached)
        except ValueError: pass
    try:
        import yfinance as yf
        rate = float(yf.Ticker("KRW=X").history(period="5d")["Close"].iloc[-1])
        _cache_set("fx:usdkrw", str(rate))
        return rate
    except Exception as e:
        log.warning("환율 조회 실패 → fallback %.1f (%s)", fallback, e)
        return fallback


# ---- VIX ----------------------------------------------------------------

def vix_close(fallback: float = 18.0) -> float:
    cached = _cache_get("vix:close", max_age_sec=2 * 3600)
    if cached:
        try: return float(cached)
        except ValueError: pass
    try:
        import yfinance as yf
        v = float(yf.Ticker("^VIX").history(period="5d")["Close"].iloc[-1])
        _cache_set("vix:close", str(v))
        return v
    except Exception as e:
        log.warning("VIX 조회 실패 → fallback %.1f (%s)", fallback, e)
        return fallback


# ---- KR 수급 (pykrx) ----------------------------------------------------

def kr_supply_streak(ticker: str, days: int = 7) -> dict:
    """외국인·기관 순매수 연속일수.
    반환: {'foreign': int, 'institution': int, 'foreign_total': int, 'institution_total': int}
    """
    key = f"kr_supply:{ticker}:{datetime.now(KST).strftime('%Y%m%d')}"
    cached = _cache_get(key, max_age_sec=4 * 3600)
    if cached:
        try: return json.loads(cached)
        except json.JSONDecodeError: pass

    try:
        from pykrx import stock as krx
    except ImportError:
        return {"foreign": 0, "institution": 0, "foreign_total": 0, "institution_total": 0}

    try:
        end = datetime.now(KST).date()
        start = end - timedelta(days=days * 3)  # 휴장일 보정
        df = krx.get_market_trading_value_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        if df is None or df.empty:
            return {"foreign": 0, "institution": 0, "foreign_total": 0, "institution_total": 0}

        # 컬럼명: '기관합계', '외국인합계' 등 — 버전마다 다를 수 있어 robust하게.
        col_foreign = next((c for c in df.columns if "외국인" in c), None)
        col_inst    = next((c for c in df.columns if "기관" in c),  None)

        f_streak = i_streak = 0
        if col_foreign:
            for v in reversed(df[col_foreign].tail(days).tolist()):
                if v > 0: f_streak += 1
                else: break
        if col_inst:
            for v in reversed(df[col_inst].tail(days).tolist()):
                if v > 0: i_streak += 1
                else: break

        result = {
            "foreign": f_streak,
            "institution": i_streak,
            "foreign_total": int(df[col_foreign].tail(days).sum()) if col_foreign else 0,
            "institution_total": int(df[col_inst].tail(days).sum()) if col_inst else 0,
        }
        _cache_set(key, json.dumps(result))
        return result
    except Exception as e:
        log.warning("kr_supply_streak failed for %s: %s", ticker, e)
        return {"foreign": 0, "institution": 0, "foreign_total": 0, "institution_total": 0}


# ---- US 어닝 서프라이즈 (Finnhub > yfinance 폴백) -------------------------

def us_earnings_surprise(ticker: str) -> Optional[float]:
    """가장 최근 분기 실적의 EPS surprise 비율(%). 실적 발표 안 됐으면 None."""
    key = f"us_earn:{ticker}:{datetime.now(KST).strftime('%Y%m%d')}"
    cached = _cache_get(key, max_age_sec=24 * 3600)
    if cached:
        try: return float(cached) if cached != "null" else None
        except ValueError: pass

    val = _us_earnings_finnhub(ticker) or _us_earnings_yfinance(ticker)
    _cache_set(key, str(val) if val is not None else "null")
    return val


def _us_earnings_finnhub(ticker: str) -> Optional[float]:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key: return None
    try:
        import requests
        r = requests.get("https://finnhub.io/api/v1/stock/earnings",
                         params={"symbol": ticker, "token": api_key}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if not data: return None
        latest = data[0]
        actual = latest.get("actual"); estimate = latest.get("estimate")
        if actual is None or estimate is None or estimate == 0: return None
        return round((actual - estimate) / abs(estimate) * 100, 2)
    except Exception as e:
        log.debug("finnhub earnings failed for %s: %s", ticker, e)
        return None


def _us_earnings_yfinance(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        df = yf.Ticker(ticker).earnings_dates
        if df is None or df.empty: return None
        recent = df.dropna(subset=["Surprise(%)"]) if "Surprise(%)" in df.columns else df.dropna()
        if recent.empty: return None
        col = "Surprise(%)" if "Surprise(%)" in recent.columns else recent.columns[-1]
        return float(recent[col].iloc[0])
    except Exception:
        return None


# ---- KR 어닝 (OpenDART) — 옵션 ------------------------------------------

def kr_earnings_surprise(ticker: str) -> Optional[float]:
    """OpenDART에서 직전 분기 영업이익 vs 컨센서스. 키 없으면 None."""
    api_key = os.getenv("OPEN_DART_KEY")
    if not api_key: return None
    # 운영 시 OpenDART rcept_no 조회 → 분기보고서 파싱으로 교체
    return None
