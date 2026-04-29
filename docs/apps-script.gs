/**
 * 데일리 픽 — Google Apps Script 웹앱
 *
 * 역할:
 *   1. GitHub Actions 가 picks.json 갱신 후 POST 로 호출
 *   2. 받은 picks 데이터를 'Picks' 시트에 행 단위로 누적 기록
 *   3. 새 갱신 알림을 NOTIFY_EMAILS 로 이메일 발송
 *
 * 배포:
 *   1) 사용자가 Google Drive 에 빈 스프레드시트 생성
 *   2) [확장 → Apps Script] 메뉴에서 본 코드 전체 붙여넣기
 *   3) NOTIFY_EMAILS 변수에 본인 이메일 입력 (콤마 구분 다중 가능)
 *   4) [배포 → 새 배포] → 유형 '웹 앱', 액세스 권한 '모든 사용자' 로 배포
 *   5) 발급된 https://script.google.com/macros/s/.../exec URL 을 GitHub Secrets 에
 *      'SHEETS_WEBHOOK_URL' 로 등록
 *
 * 보안 메모:
 *   - 'Anyone, even anonymous' 배포 = URL 알면 누구나 POST 가능.
 *   - 운영 시 OPTIONAL_TOKEN 을 설정하고 GitHub Actions 에서 동일 토큰을 함께 보내,
 *     일치하지 않으면 거부하도록 강화 권장.
 */

// ---- 사용자 설정 ----------------------------------------------------------
const NOTIFY_EMAILS = 'YOUR_EMAIL@example.com';   // 콤마 구분 다중 가능 'a@b.com, c@d.com'
const SHEET_NAME    = 'Picks';                    // 시트 이름 (없으면 자동 생성)
const OPTIONAL_TOKEN = '';                        // (선택) 보안 토큰. GitHub Secret 과 일치 시만 처리.
const SITE_URL      = 'https://songjun2625.github.io/daily-stock-alert/today.html';

// ---- 엔트리 포인트 --------------------------------------------------------

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || '{}');
    if (OPTIONAL_TOKEN && body.token !== OPTIONAL_TOKEN) {
      return _json({ ok: false, error: 'invalid token' }, 401);
    }
    appendToSheet_(body);
    sendEmail_(body);
    return _json({ ok: true });
  } catch (err) {
    Logger.log('doPost error: ' + err);
    return _json({ ok: false, error: String(err) }, 500);
  }
}

function doGet() {
  // 헬스체크용 GET. 브라우저에서 URL 열어 'OK' 보이면 배포 정상.
  return _json({ ok: true, hint: 'POST picks payload here.' });
}

// ---- 시트 누적 기록 ------------------------------------------------------

function appendToSheet_(data) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow([
      'timestamp_kst', 'market', 'ticker', 'name', 'sector',
      'price', 'change_1d_pct', 'rsi', 'score',
      'entry_low', 'entry_high', 'target', 'stoploss',
      'one_liner', 'site_url',
    ]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, 15).setFontWeight('bold').setBackground('#E0E7FF');
  }

  const ts = data.updated_at_kst || new Date().toISOString();
  for (const market of ['kr', 'us', 'futures']) {
    const m = data[market];
    if (!m || !m.picks) continue;
    for (const p of m.picks) {
      sheet.appendRow([
        ts, market, p.ticker || '', p.name || '', p.sector || '',
        p.price || 0, (p.change_pct_1d || 0).toFixed(2),
        (p.rsi || 0).toFixed(1), (p.score || 0).toFixed(1),
        p.entry_low || 0, p.entry_high || 0, p.target || 0, p.stoploss || 0,
        (p.narrative && p.narrative.one_liner) || '',
        SITE_URL,
      ]);
    }
  }
}

// ---- 이메일 발송 ---------------------------------------------------------

function sendEmail_(data) {
  if (!NOTIFY_EMAILS || NOTIFY_EMAILS === 'YOUR_EMAIL@example.com') {
    Logger.log('NOTIFY_EMAILS 미설정 — 이메일 스킵');
    return;
  }

  const subject = `[데일리 픽] ${data.updated_at_kst || ''} 종목 갱신`;
  const html = buildEmailHtml_(data);
  const text = buildEmailText_(data);

  MailApp.sendEmail({
    to: NOTIFY_EMAILS,
    subject: subject,
    body: text,
    htmlBody: html,
  });
}

function buildEmailHtml_(data) {
  const fear = data.fear || {};
  const fearRow = (label, info) => info
    ? `<tr><td style="padding:4px 12px;color:#6B7280">${label}</td>
         <td style="padding:4px 12px;font-weight:600">${info.value} ${info.light} ${info.label}</td>
         <td style="padding:4px 12px;color:#6B7280">${info.summary || ''}</td></tr>`
    : '';

  let html = `
<div style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0B1B3D">
  <h2 style="margin:0 0 4px 0">데일리 픽 — 오늘의 종목</h2>
  <div style="color:#6B7280;font-size:13px">갱신: ${data.updated_at_kst || ''}</div>
  <table style="border-collapse:collapse;margin:16px 0;font-size:13px">
    ${fearRow('🇰🇷 한국장 공포지수', fear.vkospi)}
    ${fearRow('🇺🇸 미장 공포지수', fear.vix)}
  </table>
`;

  for (const [market, label] of [['kr', '🇰🇷 코스피·코스닥'], ['us', '🇺🇸 나스닥·NYSE'], ['futures', '📊 선물·ETF']]) {
    const picks = (data[market] || {}).picks || [];
    if (!picks.length) continue;
    html += `<h3 style="margin:24px 0 8px 0;border-bottom:2px solid #1E3A8A;padding-bottom:6px">${label}</h3>`;
    for (const p of picks) {
      const cur = market === 'us' ? '$' : '';
      const priceFmt = market === 'us'
        ? `$${(p.price || 0).toFixed(2)} (≈ ${(p.price_krw || 0).toLocaleString()}원)`
        : `${(p.price || 0).toLocaleString()}원`;
      html += `
<div style="border:1px solid #EAEAF0;border-radius:12px;padding:14px;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div>
      <span style="background:#F1F5F9;color:#334155;padding:2px 8px;border-radius:999px;font-size:11px">${p.sector || market}</span>
      <strong style="font-size:16px;margin-left:6px">${p.ticker}</strong>
      <span style="color:#6B7280;font-size:13px">${(p.name || '').substring(0, 25)}</span>
    </div>
    <span style="background:#DBEAFE;color:#1E40AF;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600">${(p.score || 0).toFixed(0)}점</span>
  </div>
  <div style="margin-top:6px;font-size:18px;font-weight:700">${priceFmt}</div>
  <div style="margin-top:6px;font-size:13px;color:#374151">${(p.narrative && p.narrative.one_liner) || ((p.reasons || []).slice(0, 2).join(', '))}</div>
  <table style="width:100%;margin-top:8px;font-size:12px;border-collapse:collapse">
    <tr>
      <td style="background:#F1F5F9;padding:8px;text-align:center;border-radius:6px;width:33%">📥 진입<br/><strong>${cur}${(p.entry_low || 0).toLocaleString()}~${cur}${(p.entry_high || 0).toLocaleString()}</strong></td>
      <td style="background:#FEE2E2;padding:8px;text-align:center;border-radius:6px;color:#991B1B;width:33%">🛑 손절<br/><strong>${cur}${(p.stoploss || 0).toLocaleString()}</strong></td>
      <td style="background:#DCFCE7;padding:8px;text-align:center;border-radius:6px;color:#166534;width:33%">🎯 목표<br/><strong>${cur}${(p.target || 0).toLocaleString()}</strong></td>
    </tr>
  </table>
</div>`;
    }
  }

  html += `
  <div style="margin-top:24px">
    <a href="${SITE_URL}" style="display:inline-block;background:#0B1B3D;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">전체 보기 →</a>
  </div>
  <p style="margin-top:24px;font-size:11px;color:#9A3412;background:#FFF7ED;border:1px solid #FED7AA;padding:10px;border-radius:8px;line-height:1.6">
    본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.
    과거 수익률은 미래 수익을 보장하지 않습니다. 본 서비스는 「자본시장법」상 유사투자자문업으로 신고된 1:多 일방 발송 정보 서비스입니다.
  </p>
</div>`;
  return html;
}

function buildEmailText_(data) {
  let txt = `데일리 픽 종목 갱신\n갱신: ${data.updated_at_kst || ''}\n\n`;
  const fear = data.fear || {};
  if (fear.vkospi) txt += `🇰🇷 VKOSPI: ${fear.vkospi.value} ${fear.vkospi.label}\n`;
  if (fear.vix)    txt += `🇺🇸 VIX:    ${fear.vix.value} ${fear.vix.label}\n`;
  txt += '\n';
  for (const [m, label] of [['kr', '🇰🇷 코스피·코스닥'], ['us', '🇺🇸 나스닥·NYSE'], ['futures', '📊 선물·ETF']]) {
    const picks = (data[m] || {}).picks || [];
    if (!picks.length) continue;
    txt += `=== ${label} ===\n`;
    for (const p of picks) {
      const cur = m === 'us' ? '$' : '';
      txt += `  · ${p.ticker} ${p.name || ''} (${p.sector || m})\n`;
      txt += `    가격: ${cur}${(p.price || 0).toLocaleString()}${m === 'us' ? '' : '원'}  |  점수: ${(p.score || 0).toFixed(0)}점\n`;
      if (p.entry_low) txt += `    진입 ${cur}${p.entry_low}~${cur}${p.entry_high} / 손절 ${cur}${p.stoploss} / 목표 ${cur}${p.target}\n`;
      const oneLiner = (p.narrative && p.narrative.one_liner) || '';
      if (oneLiner) txt += `    ${oneLiner}\n`;
    }
    txt += '\n';
  }
  txt += `전체 보기: ${SITE_URL}\n\n`;
  txt += `본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.\n`;
  txt += `과거 수익률은 미래 수익을 보장하지 않습니다.\n`;
  return txt;
}

function _json(obj, code) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
