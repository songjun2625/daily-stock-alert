"""
카카오 알림톡 템플릿 빌더.

규칙:
  - 모든 메시지는 "정보성 메시지"(가입 회원 대상)로만 발송. 광고성은 별도 친구톡 + 별도 동의.
  - 발송 직전 검수기에서 다음을 자동 체크:
      a) 면책 문구 포함 여부 (LEGAL_FOOTER)
      b) 금지 단어 부재 (FORBIDDEN_TERMS)
      c) 신고번호 표기 여부
  - 카카오 알림톡 템플릿은 사전 등록·검수 필수. 본 코드는 템플릿 본문 생성 + 변수 치환만 담당.
"""
from __future__ import annotations
from datetime import datetime
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screener.config import LEGAL_FOOTER, FORBIDDEN_TERMS
from screener.screener_us import USCandidate, market_traffic_light
from screener.screener_kr import KRCandidate


# ---- 한국장 알림톡 (정보성, 매일 8:30 발송) ------------------------------

def build_kakao_message_kr(picks: list[KRCandidate], date: datetime) -> str:
    head = f"[데일리 픽] {date.strftime('%Y.%m.%d')} ({'월화수목금토일'[date.weekday()]})\n"
    body = ["■ 코스피·코스닥 관심 종목"]
    if not picks:
        body.append("· 오늘은 조건을 만족하는 종목이 없어 발송 종목이 없습니다.")
    else:
        for p in picks:
            reason = ", ".join(p.reasons[:2]) if p.reasons else "스크리너 통과"
            body.append(f"· {p.name}({p.ticker}) — {reason}")
    msg = head + "\n".join(body) + "\n\n" + LEGAL_FOOTER
    return msg


# ---- 미장 알림톡 (정보성, 매일 22:00 발송) -------------------------------
# us-swing-screener 스킬 스펙 출력 형식을 그대로 알림톡 길이에 맞게 압축.
# 카카오 알림톡 본문은 1,000자 제한 → 핵심만, 상세 매매 가이드는 마이페이지 링크.

def _kr_won(value: float) -> str:
    """원화 만원 단위 표기 — 초보자 가독성."""
    won = round(value)
    if won >= 10_000:
        return f"{won/10_000:.0f}만원"
    return f"{won:,}원"


def build_kakao_message_us(picks: list[USCandidate], date: datetime,
                           include_traffic_light: bool = True,
                           portal_url: str = "https://dailypick.kr/m") -> str:
    head = f"[데일리 픽 · 미장] {date.strftime('%Y.%m.%d')}\n"

    if include_traffic_light:
        light = market_traffic_light()
        head += f"{light['light']} 시장: {light['label']} (VIX {light['vix']}) — {light['summary']}\n"

    body = ["■ 관심 종목 (3~5일 스윙)"]
    if not picks:
        body.append("· 오늘은 조건을 만족하는 종목이 없습니다.")
    else:
        for p in picks:
            reason = ", ".join(p.reasons[:2]) if p.reasons else "스크리너 통과"
            body.append(
                f"· {p.ticker} {p.name[:14]} — {reason}\n"
                f"   현재가 ${p.price:.2f} (약 {_kr_won(p.price_krw)})\n"
                f"   진입 ${p.entry_low}~${p.entry_high} / 손절 ${p.stoploss}"
            )
    body.append(f"\n상세 매매 가이드 → {portal_url}")
    msg = head + "\n".join(body) + "\n\n" + LEGAL_FOOTER
    return msg


# ---- 검수기 (발송 전 자동 차단) ------------------------------------------

class ComplianceError(ValueError):
    pass


def lint_message(msg: str) -> None:
    """발송 직전 자동 검수 — 위반 시 예외 발생, 발송 차단."""
    for term in FORBIDDEN_TERMS:
        if term in msg:
            raise ComplianceError(f"금지 표현 검출: '{term}' — 발송 차단")
    if "투자 권유가 아니" not in msg and "투자 권유가 아니며" not in msg:
        raise ComplianceError("면책 문구 누락 — 발송 차단")
    if "신고" not in msg:
        raise ComplianceError("신고번호 누락 — 발송 차단")


# ---- 7일 무료체험 결제 D-1 안내 (전자상거래법 의무) -----------------------

def build_payment_d1_notice(name: str, plan_label: str, amount: int, charge_at: str) -> str:
    return (
        f"[데일리 픽] {name}님께 안내드립니다.\n"
        f"내일 {charge_at}에 {plan_label} 정기결제가 진행됩니다.\n"
        f"결제 금액: {amount:,}원\n"
        f"해지·환불은 마이페이지에서 즉시 가능합니다.\n\n"
        + LEGAL_FOOTER
    )


if __name__ == "__main__":
    # 샘플 출력
    from datetime import datetime
    sample_kr = [type("X", (), dict(ticker="005930", name="삼성전자",
                                    reasons=["RSI 34", "5일선 지지"]))()]
    print(build_kakao_message_kr(sample_kr, datetime.now()))
    print("---")
    sample_us = [USCandidate(
        ticker="NVDA", name="NVIDIA Corporation", price=120.0, price_krw=177600,
        change_pct_1d=2.1, market_cap=3.0e12,
        operating_margin=0.55, revenue_growth=0.85, pe_ratio=60.0, sector="Tech",
        avg_volume=200_000_000, rsi=38.0, drawdown_52w=0.18,
        macd_golden_cross=True, ma_aligned_up=True, volume_spike=True,
        score=80.0, reasons=["영업이익률 55%", "RSI 38(과매도 반등)"],
        entry_low=118.2, entry_high=120.6, target=128.0, stoploss=115.2,
    )]
    msg = build_kakao_message_us(sample_us, datetime.now())
    print(msg)
    lint_message(msg)
    print("LINT OK")
