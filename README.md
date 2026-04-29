# 데일리 픽 (Daily Pick) — 데일리 종목 알림톡 서비스

> 매일 아침 8:30 (한국장) / 밤 22:00 (미장), 알고리즘 기반 단기 스윙(3~5일) 후보 종목 3개를 카카오 알림톡으로 발송하는 1:多 일방 발송 서비스.

본 저장소는 **(주)포트존**의 신규 사업 MVP 코드입니다. 사업화 검토 리포트(2026.04.29) §1~§8을 그대로 코드로 옮겼으며, **유사투자자문업 신고**가 완료된 후에 운영을 시작해야 합니다.

---

## 0. 시작 전 반드시 (Day 1~10)

| # | 작업 | 결과물 |
|---|------|--------|
| 1 | 사업자등록 (개인 또는 법인) | 사업자등록증 |
| 2 | 통신판매업 신고 (관할 구청·시청) | 신고번호 |
| 3 | 유사투자자문업 신고 (금감원 전자민원창구) | 신고번호 (처리 30일) |
| 4 | 한국금융투자협회 보수교육 (연 1회, 8시간) | 수료증 |
| 5 | 약관·환불정책·면책조항 변호사 검토 | 검토 의견서 (50~100만원) |

> ⚠️ **신고번호 수령 전 발송 금지.** 첫 발송 전에 모든 페이지·발송물에 신고번호를 박아넣어야 합니다. 미신고 운영 시 3년 이하 징역 또는 1억 이하 벌금(자본시장법 §446).

---

## 1. 프로젝트 구조

```
daily-stock-alert/
├─ landing/                  # 정적 랜딩페이지 (GitHub Pages 또는 Framer)
│  ├─ index.html             # Hero·Pricing·Signup·FAQ
│  └─ docs/                  # 약관·개인정보·환불·면책 (필수 표기)
├─ screener/                 # 일별 자동 스크리너
│  ├─ config.py              # 시간 윈도우·임계값·금지어
│  ├─ indicators.py          # RSI, MACD, 이동평균
│  ├─ screener_us.py         # 미장 (영업이익률 20%+, RSI 30~45, 신호등, 손절가)
│  └─ screener_kr.py         # 한국장 (거래량+RSI+MACD+수급, PDF §3-3)
├─ sender/                   # 카카오 알림톡 발송
│  ├─ templates.py           # 메시지 빌더 + 자동 검수기 (lint_message)
│  └─ send_alimtalk.py       # SOLAPI 어댑터 + 시간 윈도우 강제
├─ backend/                  # FastAPI 회원·결제 API
│  ├─ main.py                # /api/subscribe, /api/cancel, /webhooks/*
│  └─ schema.sql             # Supabase 스키마 (members, send_logs, consent_logs)
├─ orchestrator.py           # run_kr / run_us / confirm
└─ .github/workflows/
   └─ daily-screener.yml     # KR 23:30 UTC / US 13:00 UTC 자동 실행
```

---

## 2. 알림톡 발송 시간 윈도우 (핵심 설계)

| 시장 | 발송 시각 (KST) | 메시지 카테고리 | 야간 차단 회피 방법 |
|------|----------------|----------------|---------------------|
| 코스피·코스닥 | **평일 08:30** | 정보성 (가입 회원 대상) | 자동 — 시간 제한 없음 |
| 나스닥·NYSE   | **평일 22:00** | 정보성 (가입 회원 대상) | 자동 — 정보성은 21~익일 8시 차단 미적용 |
| 광고성 친구톡 | 별도 동의 + 08:00~21:00 | 광고성 | 시간 윈도우 자동 강제 (`is_ad_blocked()`) |

`sender/send_alimtalk.py` 의 `is_kr_window()` / `is_us_window()` 가 발송 직전 시각을 검증하며, 윈도우 외 발송은 `force=True` 없이는 실행되지 않습니다.

---

## 3. 스크리너 로직 요약

### 미장 (`screener_us.py`)
사업화 리포트 + `us-swing-screener` 스킬 스펙을 결합:

1. **좋은 회사 필터** — 영업이익률 ≥ 20%, 매출 성장 ≥ 10%, 시총 ≥ $2B, 일평균 거래량 ≥ 100만주
2. **싸다 시그널** — RSI 30~45, 또는 52주 고점 대비 -15~-40% (저평가 반등)
3. **진입 시그널** — MACD 골든크로스 / 5·20일선 정배열 / 거래량 5일 평균 2배+
4. **점수화 + 사람 검수** — 상위 3개 → 알림톡 본문에 진입가·손절가·원화 환산 포함
5. **시장 신호등** — VIX ≤ 20 🟢 / 20~25 🟡 / >25 🔴 (포지션 70% 축소 권고)

### 한국장 (`screener_kr.py`)
PDF §3-3 그대로:
- 거래량 5일 평균 2배 이상 + RSI 30~40
- MACD 골든크로스 + 5/20일선 정배열 시작
- 어닝 서프라이즈 (시즌 외 스킵)
- 기관·외국인 5일 연속 순매수
- 점수화 → 상위 3개

---

## 4. 카피 가이드 (자동 강제)

`screener/config.FORBIDDEN_TERMS` 에 등록된 단어가 메시지에 포함되면 `lint_message()` 가 발송을 차단합니다.

**절대 금지** — 수익 보장, 원금 보장, 100% 적중, 누구나 월 1억, 전문가 추천, 확실한 수익, 반드시 오른다, 절대 손실
**필수 포함** — 면책 문구 + 신고번호 (`LEGAL_FOOTER`)

---

## 5. 로컬 개발

```bash
cd daily-stock-alert
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r screener/requirements.txt

# 1) 스크리너 단독 실행 (드라이런)
python -m screener.screener_us
python -m screener.screener_kr

# 2) 오케스트레이터 (큐에 적재만)
python orchestrator.py run_kr
python orchestrator.py run_us

# 3) 큐 검수 후 발송 (운영자 수동)
python orchestrator.py confirm queue/20260510_0820_kr.json

# 4) 백엔드 (FastAPI)
uvicorn backend.main:app --reload --port 8000
# → 랜딩 폼의 window.DAILYPICK_API = 'http://localhost:8000' 으로 연결
```

---

## 6. 배포

### 랜딩 (GitHub Pages — 기존 클라이언트 패턴과 동일)
```bash
git init && git add landing && git commit -m "init landing"
# 별도 repo 생성 후 push, Settings → Pages → Branch: main, Folder: /landing
```
또는 Framer / Webflow 에 `index.html` 마크업을 옮겨 빠르게 운영.

### 백엔드 (Railway 또는 Render)
- Python 3.11 / `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- 환경변수: `SUPABASE_URL`, `SUPABASE_KEY`, `ALLOWED_ORIGINS`, `PAYMENT_*`

### 스크리너 cron (GitHub Actions)
`.github/workflows/daily-screener.yml` 활성화. Secrets:
```
SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER, ALIMTALK_PFID
ALIMTALK_TEMPLATE_ID_KR, ALIMTALK_TEMPLATE_ID_US, ALIMTALK_TEMPLATE_ID_PAYMENT_D1
AUTO_SEND  # true 로 두면 검수 스킵 — 안정화 전에는 비워두기
```

---

## 7. 필수 환경변수 요약

| 키 | 용도 | 필수 |
|---|---|---|
| `SOLAPI_API_KEY` / `_SECRET` | 알림톡 발송 | ✅ |
| `SOLAPI_SENDER` | 발신 전화번호 | ✅ |
| `ALIMTALK_PFID` | 카카오 비즈니스 채널 PF ID | ✅ |
| `ALIMTALK_TEMPLATE_ID_*` | 사전 등록 템플릿 ID (검수 1~3영업일) | ✅ |
| `SUPABASE_URL` / `_KEY` | 회원 DB | 운영 |
| `PAYMENT_REDIRECT_BASE` | 토스페이먼츠 빌링 인가 URL | 운영 |
| `AUTO_SEND` | true 시 사람 검수 스킵 | ❌ (비추천) |

---

## 8. 90일 실행 체크리스트 (요약)

- **Day 1~30** — 신고 접수, 도메인·법인 셋업, MVP (랜딩+결제+스크리너+발송) 완성, 변호사 약관 검토
- **Day 31~60** — 신고번호 수령 즉시 모든 페이지·발송물 표기, 무료 베타 100명 모집, 첫 유료 50명
- **Day 61~90** — Meta 광고 일 100,000원 스케일, 200명 BEP, Pro 플랜 출시

---

## 9. 운영 리스크 대응

| 리스크 | 대응 |
|---|---|
| 1:1 자문 분류 | 회원 개별 응대 절대 금지. CS는 환불·계정 관련만. |
| 추천 종목 수익 부진 | 백테스트 PDF 공개, 환불 폭증 대비 CS 매뉴얼 |
| Meta 계정 정지 | 백업 광고 계정 + 카피 가이드라인 사내 문서화 |
| 스크리너 오류 | 발송 전 사람 1명 검수 의무화 (`AUTO_SEND=false`) |
| 알림톡 실패 | 이메일·SMS 폴백 (`/webhooks/alimtalk` 트리거) |

---

© 2026 PortZone Inc.
