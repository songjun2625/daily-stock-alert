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

# 시장별 표시 구조: 기술적 분석 4 + 퀀트(재무) 1 = 시장당 5종목.
# 점수 임계(KR 60 / US 80) 미달은 자동 제외 — 4개 다 채우지 못할 수도 있음.
TOP_N_KR = 4
TOP_N_US = 4

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
    """tools/optimize_us.py 스윕 (2026-01-01~04-30) 결과 J_strict 적용:
      RSI 30~40 (넓은 30~45 → 좁힘) / DD 20~35% (15~40 → 깊지도 얕지도 않은 황금구간)
      거래량 2.5배+ + SPY 50MA strict regime → 베이스 -0.37% → +23.62%, MDD -14%."""
    operating_margin_min: float = 0.20      # 영업이익률 ≥ 20%
    revenue_growth_min:  float = 0.10       # 매출 성장률 ≥ 10% (전년 대비)
    market_cap_min:      float = 2.0e9      # 시가총액 $2B 이상
    avg_volume_min:      int   = 1_000_000  # 일평균 거래량 100만주 이상
    rsi_low:             float = 30.0
    rsi_high:            float = 40.0       # 45 → 40 (선별성 강화)
    drawdown_low:        float = 0.20       # 15 → 20 (얕은 하락 제외)
    drawdown_high:       float = 0.35       # 40 → 35 (너무 깊은 하락 = fall knife 회피)
    per_discount_vs_peer: float = 0.20

US_THRESH = USSwingThresholds()

# 한국장 스크리닝 임계값 — 2026.01.01~04.30 승자 TOP 15 데이터 기반 v2 알고리즘.
# 분석 결과:
#   - 승자 73% 가 반도체(HBM·장비)/2차전지/방산 섹터에 집중 → 섹터 가산점 도입
#   - 본격 반등 직전 RSI 평균 33.9 (25%~75% [33.4, 34.4]) → 골든존 30~35 좁힘
#   - 본격 반등 직전 52주 -34.5% (25%~75% [33.4%, 35.5%]) → 깊은 드로우다운 우대
#   - 변동성 5~8% 종목들이 잘 오름 → 너무 안정적인 종목은 후순위
@dataclass(frozen=True)
class KRSwingThresholds:
    volume_multiplier_min: float = 1.5
    # RSI 영역 분할 — 30~35 강한 가산, 35~45 약한 가산, 50~65+ma_up 추세지속
    rsi_golden_low:  float = 30.0
    rsi_golden_high: float = 35.0           # 승자 평균 33.9
    rsi_low:  float = 30.0                  # 호환 — 기존 코드 참조
    rsi_high: float = 45.0
    rsi_trend_low:  float = 50.0
    rsi_trend_high: float = 65.0
    # 52w drawdown — 깊은 하락 우대 (-30~-40% 가 진짜 세일)
    drawdown_deep_low:  float = 0.30        # 승자 평균 -34.5%
    drawdown_deep_high: float = 0.45
    drawdown_low:  float = 0.10             # 호환 — 기존 코드 참조
    drawdown_high: float = 0.40
    # 신고가 breakout (52w 고점 5% 이내 + 정배열)
    drawdown_breakout_max: float = 0.05
    # 펀더멘털 — KR 평균 고려해 완화 (US 의 20%/10% 보다 낮음)
    operating_margin_min: float = 0.10
    revenue_growth_min: float = 0.05
    earnings_surprise_min: float = 0.05
    # 수급 — 5일 → 3일로 완화 (KR 은 데이 트레이딩 수급 변동 빠름)
    institutional_streak_days: int = 3
    market_cap_min_krw: float = 5.0e11      # 시총 5,000억 이상
    # 변동성 — 너무 낮은 (< 1.5%) 종목은 큰 수익 기대 어려움
    min_volatility_pct: float = 1.5

KR_THRESH = KRSwingThresholds()

# 2026 KR 핫섹터 가산점 — 승자 분석 기반.
# 사이클 / 테마가 KR 시장의 70%+ 를 결정. US 같은 펀더멘털 단독으로는 못 이김.
KR_SECTOR_BONUS: dict[str, float] = {
    "반도체-HBM":   30,
    "반도체-장비":   25,
    "반도체-검사":   25,
    "반도체-소재":   22,
    "반도체":       22,
    "전선·전력인프라": 28,    # 신규 — AI 데이터센터·송배전 테마, 거래대금 폭증 (2026)
    "2차전지":      20,
    "2차전지-소재":  20,
    "화학·배터리":   18,
    "방산":         20,
    "방산·전자":    20,
    "방산·우주":    18,
    "전자부품":     15,
    "전력기기":     20,        # 15 → 20 (AI 인프라 같은 테마라 격상)
    "AI":          15,
    "조선":        12,
    "자동차부품":   10,
    "자동차":      8,
    "바이오":      5,
    "바이오·헬스케어": 5,
}

# 시장별 손절·목표 (한국장 변동성 좁아 더 타이트하게)
@dataclass(frozen=True)
class ExitRules:
    stop_pct: float
    target_pct: float
    hold_days: int

# 백테스트 스윕 결과 (tools/optimize_kr.py, 2026-01-01~04-30):
#   기존 -2.5%/+4%/5d  → 누적 -33.63% / 승률 27.3% (KR 변동성에 손절 너무 타이트)
#   최적 -3.5%/+6%/7d  → 누적 +50.26% / 승률 44.6% (정상 변동 견디고 큰 수익 잡음)
EXIT_KR = ExitRules(stop_pct=0.035, target_pct=0.06, hold_days=7)
EXIT_US = ExitRules(stop_pct=0.04,  target_pct=0.06, hold_days=5)

# 품질 게이트 — 점수가 낮거나 공포지수가 높으면 그 날은 추천 자체를 보류.
# 강제로 3개를 채우려고 약한 신호를 끼워 넣는 것보다, '오늘은 미추천' 이 정직.
@dataclass(frozen=True)
class QualityGate:
    min_score_kr:   float = 60.0   # KR 점수 임계 — 영업이익률·매출성장 + 신호 1개 이상 필요
    min_score_us:   float = 80.0   # US 점수 임계 — 영업이익률 가점 비중이 커서 더 높게 설정
    vkospi_max:     float = 25.0   # 한국장 변동성 25 초과 시 미추천
    vix_max:        float = 25.0   # 미장 변동성 25 초과 시 미추천

QUALITY = QualityGate()

# 환율 (원화 병기용 — 실제 운영 시 매일 한국은행 API에서 갱신)
USD_KRW_FALLBACK = 1480.0
