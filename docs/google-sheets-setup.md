# Google Sheet + 다중 구독자 이메일 알림 셋업 (10분)

매일 picks.json 갱신 시:
1. **'Picks' 시트에 행 단위 누적 기록** — 시간·종목·가격·점수·진입/손절/목표·narrative
2. **'Subscribers' 시트의 활성 구독자 모두에게 이메일 자동 발송** — HTML 카드 디자인
3. **Subscribers 시트에 이메일을 추가/제거하면 다음 갱신부터 즉시 반영** (캐싱 없음)
4. 구독자별로 받고 싶은 시장(`kr,us,futures`)을 따로 설정 가능

---

## 이미 만들어둔 것

- **Google Sheet (빈 상태)**: [데일리 픽 — 종목 갱신 이력](https://docs.google.com/spreadsheets/d/1G-a1WZKCmOUMsI-HW0K3Vm1Qps09uAIiJITtukSnIRY/edit)
- **Apps Script 코드**: [`docs/apps-script.gs`](apps-script.gs) — Subscribers/Picks 시트 자동 생성 + 다중 발송 지원
- **GitHub Actions 워크플로**: 매일 갱신 후 자동으로 웹훅 호출하도록 이미 셋업됨

---

## 송준 대표님이 직접 하실 일 (한 번만, 5분)

### 1단계 — Apps Script 배포

1. 위 시트 URL 클릭해서 열기
2. 상단 메뉴 **[확장 → Apps Script]** 클릭
3. 새 탭이 열리면 기본 `Code.gs` 내용 모두 지우고, 본 저장소의 [`docs/apps-script.gs`](https://github.com/songjun2625/daily-stock-alert/blob/main/docs/apps-script.gs) 내용을 통째로 복사·붙여넣기
4. 코드 25번째 줄 정도, `ADMIN_EMAIL` 값을 본인 이메일로:
   ```javascript
   const ADMIN_EMAIL = 'songjun2625@gmail.com';
   ```
5. 💾 저장 (Cmd+S) → 프로젝트명 입력 (예: "데일리픽 웹훅")
6. 상단 함수 드롭다운에서 **`setup`** 선택 → ▶︎ **실행** 한 번 클릭
   - 권한 승인 ("확인되지 않은 앱" 경고 → [고급 → 안전하지 않음으로 이동] → 허용)
   - 시트로 돌아가면 'Subscribers' 와 'Picks' 두 시트가 자동 생성된 것 확인
7. 우측 상단 **[배포 → 새 배포]** 클릭
8. 톱니바퀴 → **유형 → 웹 앱**, 액세스 권한 **모든 사용자** 로 배포
9. 발급된 URL 복사 (예: `https://script.google.com/macros/s/AKfycbx.../exec`)

### 2단계 — GitHub Secret 등록

1. https://github.com/songjun2625/daily-stock-alert/settings/secrets/actions/new
2. Name: `SHEETS_WEBHOOK_URL`
3. Secret: 위에서 복사한 웹 앱 URL
4. **Add secret**

### 3단계 — 구독자 추가 (지금부터 자유롭게)

Subscribers 시트의 행을 늘려가며 이메일을 추가하면 됩니다:

| email | name | active | subscribed_at | markets | memo |
|-------|------|--------|---------------|---------|------|
| songjun2625@gmail.com | 송준 | ☑️ | 2026-04-30 | kr,us,futures | 관리자 |
| friend@example.com | 친구 김씨 | ☑️ | 2026-04-30 | us | 미장만 보고싶음 |
| beta@example.com | 베타 테스터 | ☐ | 2026-04-30 | kr,us | 일시 차단 |

- **active 체크박스 해제** = 일시 차단 (이메일 안 감, 데이터는 보존)
- **markets 컬럼**: `kr,us,futures` 중 원하는 시장만 콤마구분 (예: `us` 만 입력하면 미장만 받음)
- 시트 변경은 **다음 발송부터 즉시 반영** — Apps Script 가 매번 새로 읽음

---

## 동작 테스트

```bash
# 수동 트리거 (즉시 검증)
gh workflow run daily-screener.yml -f market=both
```

3분 후:
1. **Picks 시트**: 9개 행 (KR 3 + US 3 + Futures 3) 자동 추가됐는지 확인
2. **이메일 수신함**: Subscribers 시트의 활성 이메일들로 모두 도착했는지 확인
3. Apps Script 편집기 → [실행 → 로그 보기] 에서 `이메일 발송 결과: 성공 N건` 확인 가능

---

## 추가 — 구독 폼 통합 (선택)

웹앱 URL 에 GET 으로 이메일을 추가할 수도 있습니다:
```
https://script.google.com/macros/s/.../exec?email=newuser@example.com&name=홍길동&markets=kr,us
```
→ Subscribers 시트에 자동 행 추가. 향후 랜딩페이지의 가입 폼을 이 URL 로 연결하면 회원이 자가 등록 가능.

---

## Gmail 일일 한도

개인 Google 계정의 MailApp 한도:
- 일반 사용자: 100건/일 → 평일 4번 발송 × 25명 = 충분
- Workspace: 1,500건/일 → 거의 무제한

100건 초과 시 Gmail 대신 SendGrid/Mailgun 도입 필요. 현재는 충분.
