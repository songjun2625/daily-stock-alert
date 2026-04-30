"""
2026-01-01 ~ 오늘까지 KR 승자 종목 분석 — 새 알고리즘 설계용 인사이트 추출.

확장 유니버스로 KOSPI200 + KOSDAQ150 + 테마 종목까지 포함해
실제로 잘 오른 종목들의 공통 패턴을 데이터로 추출:
  1) 기간 누적 수익률 상위 N개
  2) 본격 상승 직전(rally start)의 기술적 상태:
     - RSI (저점에서 반등? 추세 지속?)
     - MA 위치 (5/20/60일선 정배열?)
     - 거래량 폭증 시점
     - 52주 고점 대비 위치
  3) 섹터 분류 — 반도체 / 2차전지 / 바이오 / AI / 게임 / 엔터 등
  4) 변동성 프로파일 (KR 평균 vs)

출력: tools/kr_winners_report.json + 콘솔 리포트
"""
from __future__ import annotations
import json, logging, sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import indicators as ind
from screener import data_sources as ds

log = logging.getLogger("analyze_kr")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

START = pd.Timestamp("2026-01-01")
TOP_N = 15

# 확장 유니버스 — 시총·테마·이슈 포함 (2026 핫섹터 반영)
EXPANDED_KR_UNIVERSE = [
    # 코스피 대형주 (메가캡)
    "005930", "000660", "207940", "373220", "005380", "035420", "035720",
    "068270", "051910", "012450", "000270", "105560", "055550", "006400",
    "017670", "015760", "009150", "003670", "032830", "066570",
    # 반도체 사이클 (HBM·AI 메모리)
    "042700",  # 한미반도체 (HBM)
    "388050",  # SFA반도체
    "058470",  # 리노공업
    "240810",  # 원익IPS
    "036930",  # 주성엔지니어링
    "095340",  # ISC
    "403870",  # HPSP
    "140860",  # 파크시스템스
    # 2차전지 / 화학
    "247540", "086520", "066970", "020150",  # 일진머티리얼즈
    "002990",  # 금호석유
    "010120",  # LS ELECTRIC
    # 바이오·헬스케어
    "028300", "196170", "091990", "064550",  # 바이오니아
    "302440",  # SK바이오사이언스
    "214450",  # 파마리서치
    "145020",  # 휴젤
    # AI·플랫폼·게임
    "035900",  # JYP Ent.
    "041510",  # SM
    "352820",  # 하이브
    "263750",  # 펄어비스
    "112040",  # 위메이드
    "036570",  # 엔씨소프트
    "251270",  # 넷마블
    # 방산·우주
    "079550",  # LIG넥스원
    "047810",  # 한국항공우주 (KAI)
    "272210",  # 한화시스템
    # 자동차 부품·전장
    "012330",  # 현대모비스
    "018880",  # 한온시스템
    # 조선·해운
    "010140",  # 삼성중공업
    "009540",  # HD한국조선해양
    "042660",  # 한화오션
    # 로보틱스·드론
    "108860",  # 셀바스AI
    "108320",  # LX세미콘
    "090430",  # 아모레퍼시픽
    "180640",  # 한진칼
]

KR_SECTOR: dict[str, str] = {
    # 메가캡
    "005930": "반도체", "000660": "반도체", "207940": "바이오", "373220": "2차전지",
    "005380": "자동차", "035420": "IT플랫폼", "035720": "IT플랫폼", "068270": "바이오",
    "051910": "화학·배터리", "012450": "방산", "000270": "자동차", "105560": "금융",
    "055550": "금융", "006400": "2차전지", "017670": "통신", "015760": "유틸리티",
    "009150": "전자부품", "003670": "철강·소재", "032830": "금융", "066570": "IT가전",
    # 반도체 사이클
    "042700": "반도체-HBM", "388050": "반도체", "058470": "반도체-소재", "240810": "반도체-장비",
    "036930": "반도체-장비", "095340": "반도체-검사", "403870": "반도체-장비", "140860": "반도체-검사",
    # 2차전지·화학
    "247540": "2차전지", "086520": "2차전지", "066970": "2차전지", "020150": "2차전지-소재",
    "002990": "화학", "010120": "전력기기",
    # 바이오
    "028300": "바이오", "196170": "바이오", "091990": "바이오", "064550": "바이오",
    "302440": "백신", "214450": "헬스케어", "145020": "헬스케어",
    # 엔터·게임
    "035900": "엔터", "041510": "엔터", "352820": "엔터", "263750": "게임",
    "112040": "게임", "036570": "게임", "251270": "게임",
    # 방산·우주
    "079550": "방산", "047810": "방산·우주", "272210": "방산·전자",
    # 자동차 부품
    "012330": "자동차부품", "018880": "자동차부품",
    # 조선
    "010140": "조선", "009540": "조선", "042660": "조선",
    # 기타
    "108860": "AI", "108320": "반도체", "090430": "화장품", "180640": "지주사",
}

KR_NAMES: dict[str, str] = {
    "005930": "삼성전자", "000660": "SK하이닉스", "207940": "삼성바이오로직스", "373220": "LG에너지솔루션",
    "005380": "현대차", "035420": "NAVER", "035720": "카카오", "068270": "셀트리온",
    "051910": "LG화학", "012450": "한화에어로스페이스", "000270": "기아", "105560": "KB금융",
    "055550": "신한지주", "006400": "삼성SDI", "017670": "SK텔레콤", "015760": "한국전력",
    "009150": "삼성전기", "003670": "포스코홀딩스", "032830": "삼성생명", "066570": "LG전자",
    "042700": "한미반도체", "388050": "SFA반도체", "058470": "리노공업", "240810": "원익IPS",
    "036930": "주성엔지니어링", "095340": "ISC", "403870": "HPSP", "140860": "파크시스템스",
    "247540": "에코프로비엠", "086520": "에코프로", "066970": "엘앤에프", "020150": "일진머티리얼즈",
    "002990": "금호석유", "010120": "LS ELECTRIC",
    "028300": "HLB", "196170": "알테오젠", "091990": "셀트리온헬스케어", "064550": "바이오니아",
    "302440": "SK바이오사이언스", "214450": "파마리서치", "145020": "휴젤",
    "035900": "JYP Ent.", "041510": "SM", "352820": "하이브", "263750": "펄어비스",
    "112040": "위메이드", "036570": "엔씨소프트", "251270": "넷마블",
    "079550": "LIG넥스원", "047810": "한국항공우주", "272210": "한화시스템",
    "012330": "현대모비스", "018880": "한온시스템",
    "010140": "삼성중공업", "009540": "HD한국조선해양", "042660": "한화오션",
    "108860": "셀바스AI", "108320": "LX세미콘", "090430": "아모레퍼시픽", "180640": "한진칼",
}


def _fetch(ticker: str) -> Optional[pd.DataFrame]:
    df = ds.fetch_history(ticker, market="kr", period_days=400)
    if df is None or df.empty or len(df) < 100:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _period_metrics(df: pd.DataFrame) -> dict | None:
    period = df[df.index >= START]
    if len(period) < 20:
        return None
    close = period["Close"]
    vol   = period["Volume"]
    ret_pct = (close.iloc[-1] / close.iloc[0] - 1) * 100
    max_close = close.cummax()
    drawdown_from_running_max = (close / max_close - 1) * 100
    max_drawdown = float(drawdown_from_running_max.min())
    # Best 5d-rally from any starting point
    rolling5 = close.pct_change(5) * 100
    best_5d = float(rolling5.max()) if not rolling5.empty else 0.0
    # 변동성 (일간 수익률 표준편차)
    volatility = float(close.pct_change().std() * 100)
    return {
        "ret_pct": float(ret_pct),
        "max_drawdown": max_drawdown,
        "best_5d": best_5d,
        "volatility": volatility,
        "start_close": float(close.iloc[0]),
        "end_close": float(close.iloc[-1]),
        "trading_days": int(len(period)),
    }


def _state_at_rally_start(df: pd.DataFrame) -> dict | None:
    """기간 내 최저점(저점) 시점의 기술적 상태 — 본격 반등 직전 시그널."""
    period = df[df.index >= START]
    if len(period) < 20:
        return None
    close = period["Close"]
    vol   = period["Volume"]
    low_idx = close.idxmin()
    low_pos = period.index.get_loc(low_idx)
    if low_pos < 14:
        return None
    sub = df.loc[:low_idx]
    if len(sub) < 60:
        return None
    rsi_at_low = float(ind.rsi(sub["Close"]).iloc[-1])
    macd_l, sig_l, _ = ind.macd(sub["Close"])
    macd_diff = float((macd_l - sig_l).iloc[-1])
    ma5  = float(sub["Close"].rolling(5).mean().iloc[-1])
    ma20 = float(sub["Close"].rolling(20).mean().iloc[-1])
    ma60 = float(sub["Close"].rolling(60).mean().iloc[-1])
    cur  = float(sub["Close"].iloc[-1])
    vol_ratio = float(sub["Volume"].iloc[-1] / sub["Volume"].iloc[-21:-1].mean()) if len(sub) >= 21 else 1.0
    # 52w high drawdown
    win = sub["Close"].tail(252) if len(sub) >= 252 else sub["Close"]
    peak = float(win.max())
    dd_52w = float((peak - cur) / peak * 100) if peak > 0 else 0.0
    return {
        "low_date": str(low_idx.date()),
        "rsi_at_low": rsi_at_low,
        "macd_minus_signal": macd_diff,
        "below_ma5_pct":  float((cur / ma5 - 1) * 100),
        "below_ma20_pct": float((cur / ma20 - 1) * 100),
        "below_ma60_pct": float((cur / ma60 - 1) * 100),
        "vol_ratio_at_low": vol_ratio,
        "drawdown_52w_pct_at_low": dd_52w,
        "rally_after_low_pct": float((close.iloc[-1] / close.loc[low_idx] - 1) * 100),
    }


def main() -> None:
    rows: list[dict] = []
    for t in EXPANDED_KR_UNIVERSE:
        df = _fetch(t)
        if df is None:
            log.warning("skip %s — no data", t)
            continue
        m = _period_metrics(df)
        if not m:
            continue
        s = _state_at_rally_start(df)
        rows.append({
            "ticker": t,
            "name": KR_NAMES.get(t, t),
            "sector": KR_SECTOR.get(t, "기타"),
            **m,
            **(s or {}),
        })

    rows.sort(key=lambda x: x["ret_pct"], reverse=True)
    winners = rows[:TOP_N]
    losers  = rows[-5:]

    print("\n" + "=" * 80)
    print(f"📈 2026-01-01 ~ 오늘 KR 승자 TOP {TOP_N}")
    print("=" * 80)
    print(f"{'순위':>4} {'티커':>6}  {'종목명':<14} {'섹터':<14} {'수익률':>8} {'MDD':>7} {'5일Best':>7} {'변동':>5}")
    for i, r in enumerate(winners, 1):
        print(f"{i:>4} {r['ticker']:>6}  {r['name']:<14} {r['sector']:<14} "
              f"{r['ret_pct']:>+7.1f}% {r['max_drawdown']:>+6.1f}% "
              f"{r['best_5d']:>+6.1f}% {r['volatility']:>4.1f}%")

    print("\n" + "=" * 80)
    print("🔎 승자들의 본격 반등 직전(저점) 상태")
    print("=" * 80)
    print(f"{'티커':>6}  {'섹터':<14} {'저점':<10} {'RSI':>5} {'52w DD':>7} {'5일선괴리':>9} {'20일선괴리':>9} {'반등폭':>7}")
    for r in winners:
        if "rsi_at_low" not in r: continue
        print(f"{r['ticker']:>6}  {r['sector']:<14} {r['low_date']:<10} "
              f"{r['rsi_at_low']:>5.1f} {r['drawdown_52w_pct_at_low']:>+6.1f}% "
              f"{r['below_ma5_pct']:>+8.1f}% {r['below_ma20_pct']:>+8.1f}% "
              f"{r['rally_after_low_pct']:>+6.1f}%")

    # 섹터 집계
    sector_counts: dict[str, list[float]] = {}
    for r in winners:
        sector_counts.setdefault(r["sector"], []).append(r["ret_pct"])
    print("\n" + "=" * 80)
    print("🏷️  승자 섹터 빈도")
    print("=" * 80)
    for sec, rets in sorted(sector_counts.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        avg = sum(rets) / len(rets)
        print(f"  {sec:<14} {len(rets)}개  평균수익률 {avg:+.1f}%")

    # RSI/MA 통계 (저점에서)
    rsi_vals = [r["rsi_at_low"] for r in winners if "rsi_at_low" in r]
    dd_vals  = [r["drawdown_52w_pct_at_low"] for r in winners if "drawdown_52w_pct_at_low" in r]
    print("\n" + "=" * 80)
    print("📊 인사이트 — 새 KR 알고리즘 설계 입력")
    print("=" * 80)
    if rsi_vals:
        print(f"  • 승자들의 저점 RSI: 평균 {np.mean(rsi_vals):.1f} / 중앙값 {np.median(rsi_vals):.1f} "
              f"/ 25%~75% [{np.percentile(rsi_vals, 25):.1f}, {np.percentile(rsi_vals, 75):.1f}]")
    if dd_vals:
        print(f"  • 저점 52주 드로우다운: 평균 {np.mean(dd_vals):.1f}% / 중앙값 {np.median(dd_vals):.1f}% "
              f"/ 25%~75% [{np.percentile(dd_vals, 25):.1f}, {np.percentile(dd_vals, 75):.1f}]")

    # JSON 저장
    out_path = Path(__file__).resolve().parent / "kr_winners_report.json"
    out_path.write_text(json.dumps({
        "period_start": str(START.date()),
        "winners": winners,
        "losers": losers,
        "all": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
