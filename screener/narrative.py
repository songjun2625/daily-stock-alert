"""
종목별 "왜 호재인지" 자연어 설명 생성기.

스크리너 후보(Candidate)를 받아 다음을 만든다:
  - one_liner   : 카드 collapsed 상태에서 보일 한 줄 요약
  - thesis      : 핵심 매수 근거 1~2문장
  - signals     : 기술적 시그널 ✅/❌ 항목별 상세 설명
  - risk        : 주의할 점
  - score_bd    : 점수 분해 (각 시그널이 몇 점 기여했는지)

초보자 어조 (us-swing-screener 스킬 스펙) — 전문용어는 괄호로 풀어 쓴다.
"""
from __future__ import annotations
from dataclasses import asdict
from typing import Any


def _pct(v: float | None, digits: int = 1) -> str:
    if v is None: return "—"
    return f"{v*100:.{digits}f}%" if abs(v) < 5 else f"{v:.{digits}f}%"


def narrate_us(c) -> dict:
    """USCandidate → narrative dict."""
    rsi_status = "과매도(반등 임박)" if c.rsi <= 35 else "약세 구간(반등 가능)" if c.rsi <= 45 else "중립"
    one_liner = f"{c.sector or '미국 주식'} 영업이익률 {c.operating_margin*100:.0f}%, " \
                f"{('어닝 서프라이즈 +' + format(c.earnings_surprise_pct, '.1f') + '% / ') if c.earnings_surprise_pct and c.earnings_surprise_pct >= 5 else ''}" \
                f"RSI {c.rsi:.0f} 반등 신호."
    thesis = (
        f"{c.name}는 매출 성장률 {_pct(c.revenue_growth, 0)} · 영업이익률 {_pct(c.operating_margin, 0)}로 "
        f"꾸준히 돈을 잘 버는 회사입니다. 52주 고점 대비 {c.drawdown_52w*100:.0f}% 떨어진 상태에서 "
        f"RSI {c.rsi:.0f}로 {rsi_status} 구간이며, "
    )
    if c.macd_golden_cross:
        thesis += "MACD 골든크로스(추세 전환 신호)가 막 발생해 단기 반등 모멘텀이 형성됐습니다."
    elif c.ma_aligned_up:
        thesis += "5/20일선 정배열(상승 추세) 시작 단계라 단기 반등 가능성이 높습니다."
    else:
        thesis += "거래량 급증으로 자금 유입이 확인됩니다."

    signals = []
    signals.append({
        "label": "영업이익률",
        "value": f"{c.operating_margin*100:.0f}%",
        "ok": c.operating_margin >= 0.20,
        "explain": "매출 100원 중 이만큼이 이익. 20% 이상이면 돈 잘 버는 회사로 분류.",
    })
    signals.append({
        "label": "매출 성장",
        "value": _pct(c.revenue_growth, 0) if c.revenue_growth else "—",
        "ok": c.revenue_growth and c.revenue_growth >= 0.10,
        "explain": "전년 대비 매출이 얼마나 늘었는지. 10% 이상이면 성장 회사.",
    })
    signals.append({
        "label": "RSI (과매도 지수)",
        "value": f"{c.rsi:.0f}",
        "ok": 30 <= c.rsi <= 45,
        "explain": "낮을수록 과매도 → 반등 가능성↑. 30~45 구간이 스윙에 좋은 진입대.",
    })
    signals.append({
        "label": "52주 고점 대비",
        "value": f"-{c.drawdown_52w*100:.0f}%",
        "ok": 0.15 <= c.drawdown_52w <= 0.40,
        "explain": "좋은 회사가 고점 대비 -15~-40% 빠졌으면 '세일 중'.",
    })
    signals.append({"label": "MACD 골든크로스", "value": "✓" if c.macd_golden_cross else "—",
                    "ok": c.macd_golden_cross,
                    "explain": "주가 추세가 반등으로 막 전환되는 신호."})
    signals.append({"label": "5/20일선 정배열", "value": "✓" if c.ma_aligned_up else "—",
                    "ok": c.ma_aligned_up,
                    "explain": "단기·중기 추세선 모두 상승 시작 → 추세 전환 확정."})
    signals.append({"label": "거래량 급증",   "value": "✓" if c.volume_spike else "—",
                    "ok": c.volume_spike,
                    "explain": "5일 평균 대비 2배+ 거래 → 큰손 자금 유입."})
    if c.earnings_surprise_pct is not None:
        signals.append({"label": "어닝 서프라이즈", "value": f"+{c.earnings_surprise_pct:.1f}%",
                        "ok": c.earnings_surprise_pct >= 5,
                        "explain": "최근 분기 EPS가 컨센서스보다 +5% 이상 상회하면 모멘텀."})

    risk = "레버리지·옵션이 아닌 일반 주식이지만, 단기 변동성이 평소보다 클 수 있습니다. " \
           "정해둔 손절가에 도달하면 감정 빼고 즉시 매도하세요."
    if c.drawdown_52w >= 0.30:
        risk = "고점 대비 큰 폭 하락한 종목 — 추가 하락 리스크 있음. 손절 라인 엄수."

    score_bd = []
    if c.operating_margin and c.operating_margin >= 0.20:
        score_bd.append({"name": "영업이익률 ≥20%", "points": round(min(25 * c.operating_margin / 0.20, 25*2.5), 1)})
    if c.revenue_growth and c.revenue_growth >= 0.10:
        score_bd.append({"name": "매출 성장 ≥10%", "points": round(min(15 * c.revenue_growth / 0.10, 15*2.5), 1)})
    if 30 <= c.rsi <= 45: score_bd.append({"name": "RSI 30~45 구간", "points": 20})
    if 0.15 <= c.drawdown_52w <= 0.40: score_bd.append({"name": "52주 -15~-40%", "points": 15})
    if c.macd_golden_cross: score_bd.append({"name": "MACD 골든크로스", "points": 10})
    if c.ma_aligned_up: score_bd.append({"name": "5/20일선 정배열", "points": 8})
    if c.volume_spike: score_bd.append({"name": "거래량 5일평균 2배+", "points": 7})
    if c.earnings_surprise_pct and c.earnings_surprise_pct >= 5:
        score_bd.append({"name": "어닝 서프라이즈 +5%↑", "points": 12})

    return {
        "one_liner": one_liner,
        "thesis": thesis,
        "signals": signals,
        "risk": risk,
        "score_breakdown": score_bd,
        "hold_days": 5,
    }


def narrate_kr(c) -> dict:
    """KR 내러티브 — US 알고리즘 이식판. 영업이익률·매출성장·드로우다운·RSI·수급 종합."""
    om = c.operating_margin
    rg = c.revenue_growth
    dd = getattr(c, "drawdown_52w", 0.0) or 0.0
    rsi_status = "과매도(반등 임박)" if c.rsi <= 35 else "약세 구간(반등 가능)" if c.rsi <= 45 else "중립"
    one_liner_parts = []
    if om: one_liner_parts.append(f"영업이익률 {om*100:.0f}%")
    if c.earnings_surprise and c.earnings_surprise >= 5:
        one_liner_parts.append(f"어닝 서프라이즈 +{c.earnings_surprise:.1f}%")
    one_liner_parts.append(f"RSI {c.rsi:.0f} 반등 신호")
    one_liner = f"{c.sector or '코스피·코스닥'} " + " / ".join(one_liner_parts)

    thesis = f"{c.name}({c.ticker})는 "
    if om and rg:
        thesis += f"매출 성장률 {rg*100:.0f}% · 영업이익률 {om*100:.0f}%로 꾸준히 돈을 잘 버는 회사입니다. "
    elif om:
        thesis += f"영업이익률 {om*100:.0f}%의 안정적 수익 구조를 가진 기업입니다. "
    if 0.10 <= dd:
        thesis += f"52주 고점 대비 {dd*100:.0f}% 떨어진 상태에서 "
    thesis += f"RSI {c.rsi:.0f}로 {rsi_status} 구간이며, "
    if c.macd_golden_cross:
        thesis += "MACD 골든크로스(추세 전환 신호)가 막 발생해 단기 반등 모멘텀이 형성됐습니다."
    elif c.ma_aligned_up:
        thesis += "5/20일선 정배열(상승 추세) 시작 단계라 단기 반등 가능성이 높습니다."
    elif c.foreign_streak >= 5 or c.institution_streak >= 5:
        who = []
        if c.foreign_streak >= 5: who.append(f"외국인 {c.foreign_streak}일")
        if c.institution_streak >= 5: who.append(f"기관 {c.institution_streak}일")
        thesis += f"{' / '.join(who)} 연속 순매수로 수급이 강해지고 있습니다."
    elif c.volume_spike:
        thesis += "거래량 1.5배+로 자금 유입이 확인됩니다."
    else:
        thesis += "기술적·펀더멘털 신호 조합으로 단기 진입 후보로 선정됐습니다."

    signals = []
    signals.append({
        "label": "영업이익률",
        "value": f"{om*100:.0f}%" if om else "—",
        "ok": bool(om and om >= 0.10),
        "explain": "매출 100원 중 이만큼이 이익. 10% 이상이면 KR 평균 이상 수익성.",
    })
    signals.append({
        "label": "매출 성장",
        "value": f"{rg*100:.0f}%" if rg else "—",
        "ok": bool(rg and rg >= 0.05),
        "explain": "전년 대비 매출 성장률. 5% 이상이면 성장 기업.",
    })
    signals.append({
        "label": "RSI (과매도 지수)",
        "value": f"{c.rsi:.0f}",
        "ok": 30 <= c.rsi <= 45,
        "explain": "30~45 구간이 스윙 진입에 좋은 저평가 구간.",
    })
    signals.append({
        "label": "52주 고점 대비",
        "value": f"-{dd*100:.0f}%",
        "ok": 0.10 <= dd <= 0.35,
        "explain": "좋은 회사가 -10~-35% 빠지면 '세일 중'.",
    })
    signals.append({"label": "MACD 골든크로스", "value": "✓" if c.macd_golden_cross else "—",
                    "ok": c.macd_golden_cross, "explain": "추세 전환의 첫 신호."})
    signals.append({"label": "5/20일선 정배열", "value": "✓" if c.ma_aligned_up else "—",
                    "ok": c.ma_aligned_up, "explain": "단기·중기 추세선 모두 상승 시작."})
    signals.append({"label": "거래량 1.5배+", "value": "✓" if c.volume_spike else "—",
                    "ok": c.volume_spike, "explain": "5일 평균 대비 1.5배+ 거래 → 큰손 자금 유입."})
    signals.append({"label": "외국인 연속 순매수", "value": f"{c.foreign_streak}일",
                    "ok": c.foreign_streak >= 5,
                    "explain": "외국인은 시장 방향성을 가장 빠르게 반영."})
    signals.append({"label": "기관 연속 순매수", "value": f"{c.institution_streak}일",
                    "ok": c.institution_streak >= 5,
                    "explain": "연기금·자산운용사 자금 유입은 안정적 상승 동력."})
    if c.earnings_surprise is not None:
        signals.append({"label": "어닝 서프라이즈",
                        "value": f"+{c.earnings_surprise:.1f}%",
                        "ok": c.earnings_surprise >= 5,
                        "explain": "최근 분기 영업이익이 컨센서스 대비 +5%↑이면 모멘텀."})

    if dd >= 0.30:
        risk = "고점 대비 큰 폭 하락한 종목 — 추가 하락 리스크 있음. 손절 라인 엄수."
    else:
        risk = ("개별 종목 리스크는 시장 전체 흐름과 무관하게 발생할 수 있습니다. "
                "정해둔 손절 라인에 도달하면 즉시 매도해 손실 확대를 막으세요.")

    score_bd = []
    if om and om >= 0.10:
        score_bd.append({"name": "영업이익률 ≥10%", "points": round(min(25 * om / 0.10, 25*2.5), 1)})
    if rg and rg >= 0.05:
        score_bd.append({"name": "매출 성장 ≥5%", "points": round(min(15 * rg / 0.05, 15*2.5), 1)})
    if 30 <= c.rsi <= 45: score_bd.append({"name": "RSI 30~45 구간", "points": 20})
    if 0.10 <= dd <= 0.35: score_bd.append({"name": "52주 -10~-35%", "points": 15})
    if c.macd_golden_cross: score_bd.append({"name": "MACD 골든크로스", "points": 10})
    if c.ma_aligned_up: score_bd.append({"name": "5/20일선 정배열", "points": 8})
    if c.volume_spike: score_bd.append({"name": "거래량 1.5배+", "points": 7})
    if c.earnings_surprise and c.earnings_surprise >= 5:
        score_bd.append({"name": "어닝 서프라이즈 +5%↑", "points": 12})
    if c.foreign_streak >= 5: score_bd.append({"name": "외국인 5일+ 연속매수", "points": 12})
    if c.institution_streak >= 5: score_bd.append({"name": "기관 5일+ 연속매수", "points": 13})

    return {
        "one_liner": one_liner,
        "thesis": thesis,
        "signals": signals,
        "risk": risk,
        "score_breakdown": score_bd,
        "hold_days": 5,
    }


def narrate_futures(c) -> dict:
    asset_label = "레버리지 ETF (변동 2~3배)" if c.leveraged else ("ETF" if "ETF" in c.name or c.ticker.startswith(("0", "1", "2", "3")) else "선물·ETF")
    one_liner = f"{asset_label}, RSI {c.rsi:.0f} {'반등' if c.rsi <= 40 else '추세'}, " + \
                ("MACD 골든크로스" if c.macd_golden_cross else ("정배열 시작" if c.ma_aligned_up else "거래량 급증"))
    thesis = f"{c.name}({c.ticker})는 RSI {c.rsi:.0f}에 위치한 {asset_label}입니다. "
    if c.macd_golden_cross: thesis += "MACD 골든크로스로 추세 전환 신호. "
    if c.ma_aligned_up:    thesis += "5/20일선 정배열이 막 형성됐습니다. "
    if c.volume_spike:     thesis += "거래량이 5일 평균의 2배+로 자금이 몰리고 있습니다. "
    if c.leveraged:
        thesis += "레버리지 상품이므로 평소보다 보수적으로 진입하세요."

    signals = [
        {"label": "RSI", "value": f"{c.rsi:.0f}", "ok": 30 <= c.rsi <= 50,
         "explain": "30~50 구간이 스윙 진입에 적합."},
        {"label": "MACD 골든크로스", "value": "✓" if c.macd_golden_cross else "—",
         "ok": c.macd_golden_cross, "explain": "추세 전환 신호."},
        {"label": "5/20일선 정배열", "value": "✓" if c.ma_aligned_up else "—",
         "ok": c.ma_aligned_up, "explain": "단기·중기 모두 상승 시작."},
        {"label": "거래량 급증", "value": "✓" if c.volume_spike else "—",
         "ok": c.volume_spike, "explain": "5일 평균 2배+ 거래."},
    ]
    risk = ("⚠️ 레버리지 상품 — 손실이 2~3배로 확대됩니다. " if c.leveraged
            else "ETF·선물도 단기 변동에 취약합니다. ") + \
           "정해둔 손절가 엄수, 한 종목 비중은 전체 자산의 " \
           f"{c.position_size_pct}% 이하로 제한하세요."

    score_bd = []
    if 30 <= c.rsi <= 50: score_bd.append({"name": "RSI 30~50", "points": 25})
    if c.macd_golden_cross: score_bd.append({"name": "MACD 골든크로스", "points": 20})
    if c.ma_aligned_up:    score_bd.append({"name": "5/20일선 정배열", "points": 15})
    if c.volume_spike:     score_bd.append({"name": "거래량 급증", "points": 15})
    if 0.05 <= c.drawdown_52w <= 0.30: score_bd.append({"name": "52주 -5~-30%", "points": 10})
    if c.leveraged: score_bd.append({"name": "레버리지 페널티", "points": -5})

    return {
        "one_liner": one_liner,
        "thesis": thesis,
        "signals": signals,
        "risk": risk,
        "score_breakdown": score_bd,
        "hold_days": c.hold_days_max,
        "position_size_pct": c.position_size_pct,
        "leveraged": c.leveraged,
    }
