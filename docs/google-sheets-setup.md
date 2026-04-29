# Google Sheet + 이메일 알림 셋업 (10분, 무료)

매일 picks.json 이 갱신될 때마다 자동으로:
1. **Google Sheet 의 'Picks' 시트에 행 단위로 누적 기록** — 각 종목, 가격, 점수, 진입/손절/목표가, narrative 한 줄 요약 등
2. **NOTIFY_EMAILS 로 이메일 발송** — HTML 카드 디자인 + plain text 둘 다

---

## 1단계 — Google Sheet 생성

1. https://sheets.new 에서 빈 스프레드시트 생성
2. 파일명을 적당히 (예: "데일리 픽 — 종목 이력")

> Apps Script 가 자동으로 'Picks' 시트를 생성하므로, 빈 상태로 두셔도 됩니다.

---

## 2단계 — Apps Script 배포

1. 위에서 만든 스프레드시트에서 **[확장 → Apps Script]** 클릭
2. 기본으로 보이는 `Code.gs` 파일 내용을 모두 지우고, 본 저장소의 [`docs/apps-script.gs`](apps-script.gs) 파일 내용을 통째로 복사·붙여넣기
3. 상단 **사용자 설정** 부분 수정:
   ```javascript
   const NOTIFY_EMAILS = '본인이메일@example.com';   // 받는 이메일 (콤마로 다중 가능)
   const OPTIONAL_TOKEN = '';                          // (선택) 보안 토큰 — 공란이면 미사용
   ```
4. 💾 저장 (Cmd/Ctrl + S)
5. 우측 상단 **[배포 → 새 배포]** 클릭
6. 톱니바퀴 → **유형 선택 → 웹 앱**
7. 다음과 같이 설정:
   - 설명: "데일리 픽 웹훅 v1"
   - 다음 사용자로 실행: **나** (송준 대표님 본인)
   - 액세스 권한: **모든 사용자** (Anyone) — GitHub Actions 가 인증 없이 POST 가능해야 함
8. **[배포]** 클릭 → 권한 승인 (본인 계정으로 sheet/mail 접근 허용)
9. 발급되는 **웹 앱 URL** 복사 — 형태: `https://script.google.com/macros/s/AKfycbx.../exec`

> 권한 승인 시 "확인되지 않은 앱" 경고가 뜨면 [고급 → 안전하지 않음으로 이동] 누르고 진행 (본인이 만든 스크립트라 안전).

---

## 3단계 — GitHub Repo Secret 등록

1. https://github.com/songjun2625/daily-stock-alert/settings/secrets/actions 접속
2. **[New repository secret]** 클릭
3. Name: `SHEETS_WEBHOOK_URL`
4. Secret: 위 2단계에서 복사한 웹 앱 URL 그대로 붙여넣기
5. **Add secret** 클릭

(선택) Slack 채널에도 같이 알림 받고 싶으면:
- Slack 워크스페이스 → 앱 → "Incoming Webhooks" 추가 → 채널 선택 → URL 복사
- GitHub Secret 으로 `SLACK_WEBHOOK_URL` 추가

---

## 4단계 — 동작 테스트

### A. Apps Script 헬스체크
브라우저에서 웹 앱 URL 직접 열어보세요. `{"ok":true,"hint":"POST picks payload here."}` JSON 이 보이면 배포 정상.

### B. 워크플로 수동 트리거로 풀 파이프라인 검증
```bash
gh workflow run daily-screener.yml -f market=both
```
이후:
1. GitHub Actions 탭에서 'Notify update' 단계가 ✅ 인지 확인
2. Google Sheet 의 'Picks' 시트에 새 행이 9개 (KR 3 + US 3 + futures 3) 추가됐는지 확인
3. 본인 이메일 받은편지함에 "[데일리 픽] ... 종목 갱신" 메일이 도착했는지 확인

---

## 시트 컬럼 스키마

| 컬럼 | 설명 | 예시 |
|------|------|------|
| timestamp_kst | 갱신 시각 | 2026.04.30 (목) 08:25 KST |
| market | 시장 구분 | kr / us / futures |
| ticker | 종목 코드 | NVDA, 005930 |
| name | 종목명 | NVIDIA Corporation |
| sector | 섹터·산업 | Technology / 반도체 |
| price | 현재가 | 120.42 |
| change_1d_pct | 전일대비 % | +2.18 |
| rsi | RSI | 38.0 |
| score | 스크리너 점수 | 92.0 |
| entry_low / entry_high | 진입 구간 | 118.20 / 120.60 |
| target | 목표가 | 128.00 |
| stoploss | 손절가 | 115.20 |
| one_liner | 한 줄 요약 | "Technology 영업이익률 55%, RSI 38 반등 신호" |
| site_url | 상세 URL | https://songjun2625.github.io/daily-stock-alert/today.html |

---

## 자주 묻는 문제

### "이메일이 안 와요"
- Apps Script 코드의 `NOTIFY_EMAILS` 값이 `YOUR_EMAIL@example.com` 그대로 인지 확인. 본인 이메일로 변경 필수.
- Apps Script 편집기에서 [실행 → sendEmail_] 누르면 권한 재승인 필요. 한 번 허용하면 이후 자동.
- Gmail 일일 한도: 100건. 매일 4번 발송이면 충분.

### "시트에 기록이 안 돼요"
- GitHub Actions 'Notify update' 단계 로그 확인. `Sheets webhook: 200 ...` 이 정상.
- 401 이면 OPTIONAL_TOKEN 불일치, 500 이면 Apps Script 내부 오류 — Apps Script [실행 기록] 메뉴에서 상세 보기.

### "배포 후 코드 수정했는데 반영이 안 돼요"
- Apps Script 는 배포 단위로 버전이 고정됨. 코드 수정 후 [배포 → 배포 관리 → 활성 배포 편집 → 버전 새로 만들기] 필요.

### "보안이 걱정돼요"
- `OPTIONAL_TOKEN` 을 임의 문자열로 설정 + GitHub Secret `SHEETS_WEBHOOK_TOKEN` 추가 + `tools/notify_update.py` 의 payload 에 token 필드 함께 보내도록 수정하면 무단 호출 방지됩니다.
