"""
공용 설정 — 시간대, 시장 일정, 알림톡 발송 윈도우.

핵심 원칙:
  - 모든 발송은 "정보성 메시지"(가입 회원 대상)로만 운영. 광고성 친구톡은 별도 동의·별도 시간대.
  - 한국장 발송: 평일 08:30 KST (장 시작 30분 전 → 시간 제한 전혀 없음)
  - 미장 발송:  평일 22:00 KST (NYSE 개장 30분~1시간 30분 전 → 정보성이므로 21시 이후도 OK)
  - 주말·공휴일은 발송하지 않는다.
"""
from dataclasses import dataclass
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET  = ZoneInfo("America/New_York")

# 알림톡 발송 윈도우 (KST)
KR_SEND_HOUR, KR_SEND_MIN = 8, 30
US_SEND_HOUR, US_SEND_MIN = 22, 0

# 광고성 메시지 차단 시간(법규): 21:00 ~ 익일 08:00 KST. 본 서비스는 정보성으로만 발송.
AD_BLOCK_START = 21
AD_BLOCK_END   = 8

# 스크리너 결과 상위 N개만 발송 (사람 검수 단계 후)
TOP_N_KR = 3
TOP_N_US = 3

# 백테스트·발송 모두에서 반드시 출력되는 면책 문구. 절대 생략 금지.
LEGAL_FOOTER = (
    "본 정보는 투자 권유가 아니며, 투자 결과는 투자자 본인에게 귀속됩니다.\n"
    "과거 수익률은 미래 수익을 보장하지 않습니다.\n"
    "유사투자자문업 신고: 제○○○○-○○○호 / 수신거부: 080-***-****"
)

# 카피 가이드: 절대 사용 금지 단어 (발송 직전 자동 검출)
FORBIDDEN_TERMS = [
    "수익 보장", "원금 보장", "100% 적중", "100% 수익",
    "누구나 월 1억", "전문가 추천", "전문가가 추천",
    "확실한 수익", "반드시 오른다", "절대 손실",
]

@dataclass(frozen=True)
class USSwingThresholds:
    """us-swing-screener 스킬 스펙을 그대로 코드화한 임계값."""
    operating_margin_min: float = 0.20      # 영업이익률 ≥ 20%
    revenue_growth_min:  float = 0.10       # 매출 성장률 ≥ 10% (전년 대비)
    market_cap_min:      float = 2.0e9      # 시가총액 $2B 이상
    avg_volume_min:      int   = 1_000_000  # 일평균 거래량 100만주 이상
    rsi_low:             float = 30.0
    rsi_high:            float = 45.0
    drawdown_low:        float = 0.15       # 52주 최고 대비 -15% ~
    drawdown_high:       float = 0.40       # ~ -40% (좋은 회사가 세일 중)
    per_discount_vs_peer: float = 0.20      # 업종 평균 대비 -20% 이상 저평가

US_THRESH = USSwingThresholds()

# 한국장 스크리닝 임계값 — US 알고리즘 (4월 +54%) 의 펀더멘털 필터를 KR 에 이식.
@dataclass(frozen=True)
class KRSwingThresholds:
    volume_multiplier_min: float = 1.5      # 거래량 5일 평균 1.5배+ (US 와 동일하게 완화)
    rsi_low: float  = 30.0
    rsi_high: float = 45.0                  # US 와 동일 (30~45 — 저평가 + 약세 구간)
    drawdown_low:  float = 0.10             # 52주 -10~-35% '세일 중'
    drawdown_high: float = 0.35
    operating_margin_min: float = 0.10      # 영업이익률 10%+ (한국 기업 평균 고려해 US 의 20% 보다 완화)
    revenue_growth_min: float = 0.05        # 매출 성장 5%+
    earnings_surprise_min: float = 0.05
    institutional_streak_days: int = 5
    market_cap_min_krw: float = 5.0e11      # 시총 5,000억 이상

KR_THRESH = KRSwingThresholds()

# 시장별 손절·목표 (한국장 변동성 좁아 더 타이트하게)
@dataclass(frozen=True)
class ExitRules:
    stop_pct: float
    target_pct: float
    hold_days: int

EXIT_KR = ExitRules(stop_pct=0.025, target_pct=0.04, hold_days=5)   # 한국장: -2.5% / +4%
EXIT_US = ExitRules(stop_pct=0.04,  target_pct=0.06, hold_days=5)   # 미장:   -4%   / +6%

# 환율 (원화 병기용 — 실제 운영 시 매일 한국은행 API에서 갱신)
USD_KRW_FALLBACK = 1480.0
