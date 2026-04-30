"""
업체별 공시·실적·DART 정보 수집기 — 무료 소스 위주.

수집 항목:
  KR (DART OpenAPI):
    - 최근 30일 공시 목록 (rcept_no, 보고서명, 일자, URL)
    - 다가오는 실적 발표 일정 (재무공시 일정 추정)
  US (yfinance + EDGAR):
    - 다음 어닝 캘린더 (yfinance.calendar)
    - 최근 8-K, 10-Q, 10-K SEC filings (EDGAR RSS)
  공통:
    - 산업 호황 시그널 (섹터 ETF 5일 모멘텀 기반)

실행:
  OPEN_DART_KEY 환경변수 필요 (https://opendart.fss.or.kr 무료 발급).
  키 없으면 KR 공시 스킵, US/yfinance 만 작동.

출력:
  landing/data/company_info.json
    {
      "207940": {
        "ticker": "207940", "market": "kr", "name": "삼성바이오로직스",
        "next_earnings": "2026-05-15",
        "recent_disclosures": [
          {"date": "2026-04-25", "title": "주요사항보고서(...)", "url": "..."},
          ...
        ],
        "operating_status": "최근 분기 호조" | "보합" | "둔화"
      },
      "PLTR": {
        "ticker": "PLTR", "market": "us", "name": "Palantir",
        "next_earnings": "2026-05-12",
        "recent_filings": [...]
      },
      ...
    }
"""
from __future__ import annotations
import os, json, sys, time, logging, ssl, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from screener.config import KST

log = logging.getLogger("company_info")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "landing" / "data"
PICKS_PATH = DATA_DIR / "picks.json"
OUT_PATH = DATA_DIR / "company_info.json"


# ---- KR DART OpenAPI ----------------------------------------------------

DART_BASE = "https://opendart.fss.or.kr/api"

# corp_code 캐시 — DART 는 ticker 가 아니라 corp_code(8자리) 기반
_CORP_CODE_CACHE: dict[str, str] = {}
_CORP_CODE_LOADED = False


def _load_dart_corp_codes(api_key: str) -> dict[str, str]:
    """corpCode.xml.zip 다운로드 → ticker → corp_code 매핑.
    캐시 파일 .cache/dart_corp_codes.json 24h."""
    global _CORP_CODE_LOADED, _CORP_CODE_CACHE
    if _CORP_CODE_LOADED:
        return _CORP_CODE_CACHE
    cache_file = Path(".cache/dart_corp_codes.json")
    if cache_file.exists():
        try:
            mtime = cache_file.stat().st_mtime
            if time.time() - mtime < 24 * 3600:
                _CORP_CODE_CACHE = json.loads(cache_file.read_text(encoding="utf-8"))
                _CORP_CODE_LOADED = True
                return _CORP_CODE_CACHE
        except Exception:
            pass
    try:
        import zipfile, io
        import xml.etree.ElementTree as ET
        url = f"{DART_BASE}/corpCode.xml?crtfc_key={api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "DailyPick"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with zf.open("CORPCODE.xml") as f:
                tree = ET.parse(f)
        root = tree.getroot()
        out: dict[str, str] = {}
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code  = (item.findtext("corp_code") or "").strip()
            if stock_code and stock_code != " ":
                out[stock_code] = corp_code
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        log.info("DART corp_code 매핑 %d개 캐싱", len(out))
        _CORP_CODE_CACHE = out
        _CORP_CODE_LOADED = True
        return out
    except Exception as e:
        log.warning("DART corp_code 다운로드 실패: %s", e)
        _CORP_CODE_LOADED = True
        return {}


# 보고서명 → 사용자 친화 라벨 (호재/악재 시그널)
_DART_LABEL_HINTS = {
    "주요사항보고서(자기주식취득결정)": "🟢 자사주 매입",
    "주요사항보고서(자기주식소각결정)": "🟢 자사주 소각",
    "유상증자결정": "🟡 유상증자",
    "주요사항보고서(유상증자결정)": "🟡 유상증자",
    "현금·현물배당결정": "🟢 배당 결정",
    "단일판매·공급계약체결": "🟢 신규 수주",
    "타법인주식및출자증권취득결정": "🟢 M&A·인수",
    "분기보고서": "📊 분기 실적",
    "반기보고서": "📊 반기 실적",
    "사업보고서": "📊 연간 실적",
    "주요사항보고서": "📋 주요사항",
    "최대주주변경": "⚠️ 최대주주 변경",
    "최대주주의소유주식변동신고서": "⚠️ 대주주 매도",
    "감사인의감사보고서": "📋 감사보고서",
}


def _dart_label(title: str) -> str:
    """공시 제목 → 사용자 친화 라벨 (가능하면)."""
    for key, hint in _DART_LABEL_HINTS.items():
        if key in title:
            return hint
    return "📋"


def fetch_kr_disclosures(ticker: str, api_key: str, days: int = 30) -> list[dict]:
    """DART 최근 공시 — list.json 호출."""
    corp_map = _load_dart_corp_codes(api_key)
    corp_code = corp_map.get(ticker)
    if not corp_code:
        return []
    end = datetime.now(KST).date()
    start = end - timedelta(days=days)
    url = (f"{DART_BASE}/list.json?crtfc_key={api_key}"
           f"&corp_code={corp_code}"
           f"&bgn_de={start.strftime('%Y%m%d')}"
           f"&end_de={end.strftime('%Y%m%d')}"
           f"&page_count=10")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DailyPick"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.debug("DART list.json failed for %s: %s", ticker, e)
        return []
    items = []
    for d in (data.get("list") or [])[:5]:
        rcept_no = d.get("rcept_no", "")
        rcept_dt = d.get("rcept_dt", "")
        title = d.get("report_nm", "")
        viewer = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else ""
        items.append({
            "date": rcept_dt[:4] + "-" + rcept_dt[4:6] + "-" + rcept_dt[6:8] if len(rcept_dt) == 8 else rcept_dt,
            "title": title,
            "label": _dart_label(title),
            "url": viewer,
        })
    return items


# ---- US — yfinance.calendar + earnings_dates ----------------------------

def fetch_us_company_info(ticker: str) -> dict:
    """yfinance Ticker — 다음 어닝 일정 + 최근 어닝 surprise."""
    out: dict = {}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is not None and not (hasattr(cal, "empty") and cal.empty):
            try:
                # yfinance 1.3+ 는 dict, 이전은 DataFrame
                if isinstance(cal, dict):
                    earnings_dates = cal.get("Earnings Date", [])
                    if earnings_dates:
                        out["next_earnings"] = str(earnings_dates[0])[:10]
                else:
                    if "Earnings Date" in cal.index:
                        ed = cal.loc["Earnings Date"]
                        out["next_earnings"] = str(ed.iloc[0])[:10] if hasattr(ed, "iloc") else str(ed)[:10]
            except Exception:
                pass
        # 최근 어닝 surprise — 이미 picks 에 들어있을 수 있음
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                surp_col = "Surprise(%)" if "Surprise(%)" in ed.columns else None
                if surp_col:
                    recent = ed.dropna(subset=[surp_col]).iloc[0]
                    out["last_earnings_surprise_pct"] = float(recent[surp_col])
                    out["last_earnings_date"] = str(recent.name)[:10]
        except Exception:
            pass
    except Exception as e:
        log.debug("yfinance company info failed for %s: %s", ticker, e)
    return out


# ---- 산업 호황 시그널 ----------------------------------------------------

# 섹터별 대표 ETF (KR / US)
SECTOR_ETF_MAP = {
    # KR (KOSPI 섹터 인덱스 ETF)
    "반도체": "069500", "반도체-HBM": "069500",
    "2차전지": "364980",   # TIGER 2차전지테마
    "방산": "449450",     # PLUS K방산 (가상; 운영 시 실제 ETF로 교체)
    "바이오·헬스케어": "227560",  # TIGER 200헬스케어
    # US
    "Technology": "XLK", "Communication Services": "XLC",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV",
    "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def industry_momentum(ticker_or_etf: str, market: str) -> Optional[float]:
    """섹터 ETF 5일 모멘텀 % — 양수면 호황, 음수면 둔화."""
    try:
        from screener import data_sources as ds
        df = ds.fetch_history(ticker_or_etf, market=market, period_days=30)
        if df is None or df.empty or len(df) < 6:
            return None
        close = df["Close"]
        return float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
    except Exception:
        return None


# ---- main ---------------------------------------------------------------

def main() -> None:
    if not PICKS_PATH.exists():
        log.error("picks.json 없음 — 스크리너 먼저 실행해주세요"); return

    picks_data = json.loads(PICKS_PATH.read_text(encoding="utf-8"))
    api_key = os.getenv("OPEN_DART_KEY", "").strip()
    if not api_key:
        log.warning("OPEN_DART_KEY 미설정 — KR 공시 수집 스킵, US 어닝만 수집")
    else:
        # corp_code 매핑 미리 로드 (캐싱 24h)
        _load_dart_corp_codes(api_key)

    out: dict[str, dict] = {}

    # 모든 picks (kr/us 의 picks + quant_pick) 수집
    targets: list[tuple[str, str, str, str]] = []   # (ticker, market, name, sector)
    for market in ("kr", "us"):
        block = picks_data.get(market) or {}
        for p in (block.get("picks") or []):
            targets.append((p.get("ticker"), market, p.get("name", ""), p.get("sector", "")))
        qp = block.get("quant_pick")
        if qp:
            targets.append((qp.get("ticker"), market, qp.get("name", ""), qp.get("sector", "")))

    log.info("📋 종목 %d개 공시·실적 수집 시작", len(targets))

    for ticker, market, name, sector in targets:
        if not ticker: continue
        info: dict = {"ticker": ticker, "market": market, "name": name, "sector": sector}
        if market == "kr" and api_key:
            disclosures = fetch_kr_disclosures(ticker, api_key)
            if disclosures:
                info["recent_disclosures"] = disclosures
            time.sleep(0.15)   # rate limit 보호 (10/sec)
        elif market == "us":
            yf_info = fetch_us_company_info(ticker)
            info.update(yf_info)
        # 섹터 ETF 모멘텀
        etf = SECTOR_ETF_MAP.get(sector)
        if etf:
            mom = industry_momentum(etf, market="us" if etf.startswith(("X","Q","S")) else "kr")
            if mom is not None:
                info["sector_momentum_5d_pct"] = round(mom, 2)
                info["sector_status"] = ("🔥 강세" if mom >= 3 else
                                          "📈 호조" if mom >= 1 else
                                          "➡️ 보합" if mom >= -1 else
                                          "📉 약세" if mom >= -3 else "❄️ 부진")
        out[ticker] = info
        log.info("  %s (%s): 공시 %d건, 어닝 %s, 섹터모멘텀 %s",
                 ticker, market,
                 len(info.get("recent_disclosures") or []),
                 info.get("next_earnings", "—"),
                 info.get("sector_status", "—"))

    out["_meta"] = {
        "generated_at_iso": datetime.now(KST).isoformat(timespec="seconds"),
        "tickers_count": len([k for k in out if not k.startswith("_")]),
        "dart_enabled": bool(api_key),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("✅ company_info.json 작성: %d종목", out["_meta"]["tickers_count"])


if __name__ == "__main__":
    main()
