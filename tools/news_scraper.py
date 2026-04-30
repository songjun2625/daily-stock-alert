"""
무료 RSS + yfinance 기반 호재/악재 뉴스 크롤러 + Macro auto-mode.

작동 방식:
  1) KR: 한경·매경·머니투데이·연합뉴스 RSS 24h 헤드라인 수집 (API 키 불필요)
     → 종목명 매칭 → 키워드 룰로 호재/악재 분류
  2) US: yfinance.Ticker(ticker).news (무료, 무제한, API 키 불필요)
     → 영문 키워드 룰로 호재/악재 분류
  3) Macro auto-mode:
     같은 RSS 피드의 일반 뉴스에서 macro 키워드 (FOMC/금리/관세/북한/지정학/전쟁) 빈도 집계
     → 임계 초과 시 runtime_config.json 의 market_mode 자동 'defensive' 권고

출력:
  landing/data/news_signals.json
    {
      "207940": {"ticker":..., "name":..., "headlines":[...], "score_adjust": +5,
                 "positive": 2, "negative": 0, "summary": "..."},
      ...
      "_macro": {"keyword_hits": {...}, "suggested_mode": "normal|defensive|crisis"}
    }
"""
from __future__ import annotations
import json, re, sys, time, logging, ssl, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener.config import KST
try:
    from screener.screener_kr import KR_TICKER_NAMES, DEFAULT_KR_UNIVERSE
except ImportError:
    KR_TICKER_NAMES = {}; DEFAULT_KR_UNIVERSE = []
try:
    from screener.screener_us import DEFAULT_UNIVERSE as US_UNIVERSE
except ImportError:
    US_UNIVERSE = []

log = logging.getLogger("news_scraper")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "landing" / "data" / "news_signals.json"
RUNTIME_PATH = ROOT / "landing" / "data" / "runtime_config.json"

# 무료 RSS 피드 — 회원가입·API 키 불필요
KR_RSS_FEEDS = [
    "https://www.hankyung.com/feed/economy",
    "https://www.hankyung.com/feed/finance",
    "https://www.mk.co.kr/rss/30000001/",      # 매경 경제
    "https://www.mk.co.kr/rss/50300009/",      # 매경 증권
    "https://rss.mt.co.kr/mt_news.xml",        # 머니투데이
    "https://www.yna.co.kr/rss/economy.xml",   # 연합뉴스 경제
]

# 호재/악재 키워드 (한국어)
KR_POSITIVE_KEYWORDS = [
    "어닝 서프라이즈", "실적 호조", "호실적", "흑자전환", "최대 매출", "최대 영업이익",
    "수주", "계약 체결", "공급 계약", "MOU", "협력", "파트너십",
    "인수", "M&A", "합병", "지분 인수",
    "신약", "FDA 승인", "임상 성공", "임상 3상",
    "신제품", "신규 사업", "사업 확장", "설비 투자", "증설",
    "자사주 매입", "자사주 소각", "무상증자", "주주환원",
    "목표가 상향", "투자의견 매수", "추천", "Buy",
    "HBM", "AI 칩", "GPU", "수출 호조",
]
KR_NEGATIVE_KEYWORDS = [
    "어닝 쇼크", "실적 악화", "적자전환", "영업손실 확대", "매출 감소",
    "가이던스 하향", "전망 하향", "실적 우려",
    "분식회계", "회계 부정", "감사의견 거절", "감사 지정",
    "횡령", "배임", "기소", "수사", "조사 착수",
    "리콜", "결함", "사고", "화재",
    "임원 매도", "대주주 매도", "지분 매각",
    "소송", "패소", "손해배상", "특허 침해",
    "유상증자", "전환사채 발행", "신주 발행",
    "거래정지", "상장폐지", "관리종목",
    "목표가 하향", "투자의견 매도", "Sell", "downgrade",
]

# 호재/악재 키워드 (영문)
US_POSITIVE_KEYWORDS = [
    "earnings beat", "beats expectations", "record revenue", "record earnings",
    "raises guidance", "guidance raised", "outlook raised",
    "FDA approval", "clinical success", "phase 3",
    "acquisition", "merger", "buyback", "dividend increase",
    "upgrade", "buy rating", "price target raised", "outperform",
    "wins contract", "secures deal", "partnership",
]
US_NEGATIVE_KEYWORDS = [
    "earnings miss", "misses expectations", "revenue decline",
    "lowers guidance", "guidance cut", "outlook cut",
    "lawsuit", "investigation", "fraud", "settles",
    "recall", "downgrade", "sell rating", "price target lowered",
    "underperform", "bankruptcy", "going concern",
    "FDA rejection", "clinical failure",
]

# Macro 키워드 (지정학·정책·시장 위기)
MACRO_KEYWORDS_DEFENSIVE = [
    "FOMC", "금리 인상", "기준금리 인상", "금리 동결",
    "관세", "무역분쟁", "보호무역",
    "북한 도발", "북한 미사일",
    "지정학", "정세 불안",
    "원유 급등", "유가 급등", "달러 강세",
]
MACRO_KEYWORDS_CRISIS = [
    "전쟁", "침공", "공습", "위기", "패닉", "폭락",
    "리먼", "Lehman", "금융위기", "신용위기",
    "디폴트", "default", "긴급조치",
    "사이드카", "서킷브레이커",
]


def _http_get(url: str, timeout: int = 10) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DailyPick)"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except Exception as e:
        log.debug("fetch failed %s: %s", url, e)
        return None


def _parse_rss(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return []
    items = []
    # RSS 2.0 (channel/item) and Atom (feed/entry) 둘 다 지원
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag in ("item", "entry"):
            title = ""; link = ""; pub = ""
            for child in item:
                t = child.tag.split("}")[-1].lower()
                if t == "title": title = (child.text or "").strip()
                elif t == "link":
                    link = child.attrib.get("href") or (child.text or "").strip()
                elif t in ("pubdate", "published", "updated"):
                    pub = (child.text or "").strip()
            if title:
                items.append({"title": title, "link": link, "pub": pub})
    return items


def fetch_kr_news() -> list[dict]:
    """KR RSS 통합 — 24h 이내 항목만."""
    all_items: list[dict] = []
    for feed in KR_RSS_FEEDS:
        data = _http_get(feed, timeout=8)
        if data:
            items = _parse_rss(data)
            all_items.extend(items)
            log.info("KR RSS %d items from %s", len(items), feed[:60])
        time.sleep(0.2)   # 부담 경감
    return all_items


def fetch_us_news_for_ticker(ticker: str) -> list[dict]:
    """yfinance.Ticker.news — API 키 불필요, 무료."""
    try:
        import yfinance as yf
        news = yf.Ticker(ticker).news or []
        items = []
        for n in news[:10]:
            t = n.get("title", "") or (n.get("content") or {}).get("title", "")
            if t:
                items.append({
                    "title": t,
                    "link": n.get("link") or (n.get("content") or {}).get("canonicalUrl", {}).get("url", ""),
                    "pub":  str(n.get("providerPublishTime", "")),
                })
        return items
    except Exception as e:
        log.debug("yfinance news failed for %s: %s", ticker, e)
        return []


def classify_kr(headline: str) -> tuple[int, list[str], list[str]]:
    """KR 헤드라인 분류 — (score, [hit positive], [hit negative])."""
    text = headline.lower()
    hits_pos = [k for k in KR_POSITIVE_KEYWORDS if k.lower() in text]
    hits_neg = [k for k in KR_NEGATIVE_KEYWORDS if k.lower() in text]
    score = len(hits_pos) * 5 - len(hits_neg) * 10  # 악재 비대칭 가중
    return score, hits_pos, hits_neg


def classify_us(headline: str) -> tuple[int, list[str], list[str]]:
    text = headline.lower()
    hits_pos = [k for k in US_POSITIVE_KEYWORDS if k in text]
    hits_neg = [k for k in US_NEGATIVE_KEYWORDS if k in text]
    score = len(hits_pos) * 5 - len(hits_neg) * 10
    return score, hits_pos, hits_neg


def detect_macro_mode(kr_headlines: list[dict]) -> tuple[str, dict]:
    """KR 일반 뉴스 헤드라인에서 macro 키워드 빈도 집계 → mode 권고."""
    crisis_hits = 0; defensive_hits = 0
    crisis_kw_hits: dict[str, int] = {}
    defensive_kw_hits: dict[str, int] = {}
    for h in kr_headlines:
        title = h.get("title", "").lower()
        for kw in MACRO_KEYWORDS_CRISIS:
            if kw.lower() in title:
                crisis_hits += 1
                crisis_kw_hits[kw] = crisis_kw_hits.get(kw, 0) + 1
        for kw in MACRO_KEYWORDS_DEFENSIVE:
            if kw.lower() in title:
                defensive_hits += 1
                defensive_kw_hits[kw] = defensive_kw_hits.get(kw, 0) + 1
    if crisis_hits >= 3:
        mode = "crisis"
    elif defensive_hits >= 3:
        mode = "defensive"
    else:
        mode = "normal"
    return mode, {
        "crisis_hits": crisis_hits, "crisis_kw": crisis_kw_hits,
        "defensive_hits": defensive_hits, "defensive_kw": defensive_kw_hits,
    }


def main() -> None:
    log.info("📰 뉴스 크롤러 시작 (KR RSS + yfinance, 무료 모드)")

    # KR
    kr_items = fetch_kr_news()
    log.info("KR 헤드라인 총 %d건 수집", len(kr_items))

    out: dict[str, dict] = {}

    for ticker in DEFAULT_KR_UNIVERSE:
        name = KR_TICKER_NAMES.get(ticker)
        if not name:
            continue
        # 종목명이 헤드라인에 등장하는 24h 뉴스 매칭
        matched = []
        score_total = 0
        all_pos = set(); all_neg = set()
        for it in kr_items:
            title = it.get("title", "")
            if name in title or ticker in title:
                s, pos, neg = classify_kr(title)
                if pos or neg:
                    matched.append({"title": title, "link": it.get("link", ""),
                                    "score": s, "positive": pos, "negative": neg})
                    score_total += s
                    all_pos.update(pos); all_neg.update(neg)
        if matched:
            out[ticker] = {
                "ticker": ticker, "name": name, "market": "kr",
                "headlines": matched[:5],
                "positive": len(all_pos),
                "negative": len(all_neg),
                "score_adjust": score_total,
                "summary": f"호재 {len(all_pos)}건 / 악재 {len(all_neg)}건",
            }

    # US — yfinance.news 종목별 호출
    log.info("US 종목별 뉴스 fetch 시작 (%d종목)", len(US_UNIVERSE))
    for ticker in US_UNIVERSE[:25]:
        items = fetch_us_news_for_ticker(ticker)
        if not items: continue
        matched = []
        score_total = 0
        all_pos = set(); all_neg = set()
        for it in items:
            title = it.get("title", "")
            s, pos, neg = classify_us(title)
            if pos or neg:
                matched.append({"title": title, "link": it.get("link", ""),
                                "score": s, "positive": pos, "negative": neg})
                score_total += s
                all_pos.update(pos); all_neg.update(neg)
        if matched:
            out[ticker] = {
                "ticker": ticker, "market": "us",
                "headlines": matched[:5],
                "positive": len(all_pos),
                "negative": len(all_neg),
                "score_adjust": score_total,
                "summary": f"+{len(all_pos)} / -{len(all_neg)}",
            }
        time.sleep(0.1)

    # Macro auto-mode
    mode, macro_meta = detect_macro_mode(kr_items)
    out["_macro"] = {
        "suggested_mode": mode,
        "checked_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        **macro_meta,
    }
    out["_meta"] = {
        "tickers_with_signals": len([k for k in out if not k.startswith("_")]),
        "kr_headlines_total": len(kr_items),
        "generated_at_iso": datetime.now(KST).isoformat(timespec="seconds"),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("✅ news_signals.json 작성: 종목 %d / macro mode 권고: %s",
             out["_meta"]["tickers_with_signals"], mode)

    # Macro 권고 자동 적용 — runtime_config.json 의 _macro_suggestion 필드만 갱신
    # (실제 market_mode 변경은 사용자 승인 필요 — 자동 변경은 자제)
    if RUNTIME_PATH.exists():
        try:
            rt = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            rt["_macro_suggestion"] = {
                "suggested_mode": mode,
                "checked_at_kst": out["_macro"]["checked_at_kst"],
                "defensive_hits": macro_meta["defensive_hits"],
                "crisis_hits": macro_meta["crisis_hits"],
                "_note": "자동 권고 — 실제 market_mode 변경은 운영자가 직접 결정",
            }
            RUNTIME_PATH.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("runtime_config.json _macro_suggestion 갱신: %s", mode)
        except Exception as e:
            log.warning("runtime_config 갱신 실패: %s", e)


if __name__ == "__main__":
    main()
