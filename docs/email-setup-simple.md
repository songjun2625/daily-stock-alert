# Gmail 이메일 알림 셋업 (Apps Script 불필요, 5분)

> Apps Script 가 "This app is blocked" 로 막히는 경우의 **대안 셋업**.
> Workspace 계정·고급 보호 모드와 무관하게 작동합니다.

매일 picks.json 갱신 시:
- 공개 구글시트의 **Subscribers 행을 매번 새로 읽어** 활성 구독자 전원에게 발송
- Gmail SMTP 로 직접 발송 (Google OAuth 앱 승인 불필요)
- 구독자별 markets 필터링 (`kr,us,futures`)

---

## 1단계 — Gmail 앱 비밀번호 발급 (2분)

> ⚠️ 본 비밀번호는 송준 대표님 Gmail 계정 비밀번호와 **다른** 16자리 코드입니다.
> Gmail 본 계정 비밀번호를 GitHub 에 절대 입력하지 마세요.

**전제 조건**: 2단계 인증(2-Step Verification) 활성화되어 있어야 함. 안 되어 있으면 [https://myaccount.google.com/security](https://myaccount.google.com/security) 에서 먼저 활성화.

1. https://myaccount.google.com/apppasswords 접속 (앱 비밀번호 페이지)
2. "앱 이름" 에 `DailyPick GitHub Actions` 입력
3. **[만들기]** 클릭
4. 노란 박스에 16자리 비밀번호가 표시됨 (공백 포함 `xxxx xxxx xxxx xxxx`)
5. **공백을 모두 제거**하고 16자리만 복사 (예: `abcdwxyz12345678`)
6. 이 비밀번호는 한 번만 표시됨 — 안전한 곳에 임시 저장

---

## 2단계 — 구독자 시트 공개 + Subscribers 탭 생성 (2분)

이미 만들어둔 [데일리 픽 시트](https://docs.google.com/spreadsheets/d/1G-a1WZKCmOUMsI-HW0K3Vm1Qps09uAIiJITtukSnIRY/edit) 에서:

### 2-1. Subscribers 시트 만들기

1. 시트 하단 **+ (시트 추가)** 클릭 → 시트 이름을 `Subscribers` 로 변경
2. 1행 헤더 입력:

| A1: email | B1: name | C1: active | D1: markets | E1: memo |
|-----------|----------|------------|-------------|----------|

3. 2행부터 구독자 데이터 입력:

| email | name | active | markets | memo |
|-------|------|--------|---------|------|
| songjun2625@gmail.com | 송준 | TRUE | kr,us,futures | 관리자 |
| friend@example.com | 김친구 | TRUE | us | 미장만 |
| beta@example.com | 베타 | FALSE | kr | 일시 차단 |

> `active` 컬럼은 `TRUE/FALSE` 또는 빈칸(=TRUE 처리). 체크박스 쓰고 싶으면 [삽입 → 체크박스] 도 OK.
> `markets` 빈칸이면 모든 시장을 받음.

### 2-2. 시트를 공개 (링크 있는 사람 모두 읽기 가능)

1. 우측 상단 **[공유]** 클릭
2. "일반 액세스" 섹션을 **"제한됨"** → **"링크가 있는 모든 사용자"** 로 변경
3. 권한은 **"뷰어"** (편집자 X — 안전을 위해 읽기 전용)
4. **[완료]**

### 2-3. CSV export URL 만들기

다음 형식으로 URL 을 만듭니다 (이미 만들어두셨으면 활용):

```
https://docs.google.com/spreadsheets/d/{시트ID}/gviz/tq?tqx=out:csv&sheet=Subscribers
```

본 시트 기준 정확한 URL:

```
https://docs.google.com/spreadsheets/d/1G-a1WZKCmOUMsI-HW0K3Vm1Qps09uAIiJITtukSnIRY/gviz/tq?tqx=out:csv&sheet=Subscribers
```

위 URL 을 브라우저에 붙여넣어 CSV 가 다운로드되면 정상 (만약 권한 오류 뜨면 2-2 다시 확인).

---

## 3단계 — GitHub Secrets 등록 (1분)

[https://github.com/songjun2625/daily-stock-alert/settings/secrets/actions](https://github.com/songjun2625/daily-stock-alert/settings/secrets/actions) 에서 **3개** secret 추가:

| Name | Value |
|------|-------|
| `GMAIL_USER` | `songjun2625@gmail.com` (발신할 본인 Gmail) |
| `GMAIL_APP_PASSWORD` | 위 1단계에서 복사한 16자리 (공백 없이) |
| `SUBSCRIBERS_SHEET_CSV` | 위 2-3 의 CSV export URL |

---

## 4단계 — 동작 테스트

```bash
gh workflow run daily-screener.yml -f market=both
```

3분 후 송준 대표님 이메일 + 시트의 다른 구독자 이메일에 도착하는지 확인:
- HTML 카드 디자인 (시장별 + 진입/손절/목표 + 점수)
- 구독자별 markets 필터 적용

문제 발생 시 GitHub Actions 탭의 **Send emails to subscribers** 단계 로그 확인:
- `구독자 N명 fetch` — 시트 읽기 성공
- `발송 완료: 성공 X / 실패 Y` — SMTP 결과

---

## 일일 발송 한도

Gmail SMTP 한도:
- 개인 Gmail: **500건/일** (앱 비밀번호 사용 시)
- Workspace: **2,000건/일**

평일 4번 × 100명 = 400건 → 개인 Gmail 한도 안에서 충분. 100명 넘으면 SendGrid/Mailgun 으로 이전 권장.

---

## 보안 메모

- 시트는 **읽기 전용**으로만 공개 (편집권한 X). 구독자 추가는 송준 대표님이 시트 직접 편집.
- 앱 비밀번호는 GitHub Secret 에만 저장. 코드·로그·티켓에 노출 금지.
- 노출 의심 시 즉시 [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) 에서 [삭제] 후 재발급.
- 특정 구독자가 이메일을 받기 싫어할 경우: `active` 컬럼을 FALSE 로 변경 (다음 발송부터 즉시 제외).
