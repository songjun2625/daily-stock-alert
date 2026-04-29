/**
 * 데일리 픽 — Google Apps Script 웹앱 (구독자 목록 + 시트 누적 + 다중 이메일 발송)
 *
 * 시트 구조 (자동 생성됨):
 *   - 'Subscribers' : 이메일 구독자 목록. 송준 대표님이 직접 행 추가/제거하면 즉시 반영.
 *       email | name | active | subscribed_at | markets | memo
 *   - 'Picks'       : 매일 발송된 종목 이력 누적.
 *
 * 처음 1회만 실행:
 *   - Apps Script 편집기 상단 함수 드롭다운에서 'setup' 선택 → ▶︎ 실행
 *   - Subscribers / Picks 두 시트가 자동 생성되고, ADMIN_EMAIL 이 첫 행으로 등록됨.
 *
 * 그 다음부터는 Subscribers 시트에 이메일을 행 단위로 계속 추가만 하면 됩니다.
 *   active 컬럼을 FALSE 로 바꾸면 일시 차단 (이메일 발송 안 됨, 데이터는 보존).
 *
 * 배포:
 *   [배포 → 새 배포] → 유형 '웹 앱' → 액세스 권한 '모든 사용자'
 *   발급된 URL 을 GitHub Secrets 의 SHEETS_WEBHOOK_URL 로 등록.
 */

// ---- 사용자 설정 (한 번만) -----------------------------------------------
const ADMIN_EMAIL    = 'songjun2625@gmail.com';   // 첫 setup 시 자동 등록되는 관리자 이메일
const SITE_URL       = 'https://songjun2625.github.io/daily-stock-alert/today.html';
const OPTIONAL_TOKEN = '';                          // (선택) 보안 토큰. GitHub Secret 과 일치 시만 처리.

// ---- 엔트리 포인트 -------------------------------------------------------

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents || '{}');
    if (OPTIONAL_TOKEN && body.token !== OPTIONAL_TOKEN) {
      return _json({ ok: false, error: 'invalid token' });
    }

    // 액션 분기 — 'subscribe' 면 구독자 추가, 그 외엔 picks 갱신 알림.
    if (body.action === 'subscribe') {
      const result = addSubscriber_(body);
      return _json({ ok: true, result: result });
    }

    appendToPicks_(body);
    const sent = sendEmailsToAll_(body);
    return _json({ ok: true, emails_sent: sent });
  } catch (err) {
    Logger.log('doPost error: ' + err);
    return _json({ ok: false, error: String(err) });
  }
}

function doGet(e) {
  // 헬스체크 + 간단 구독 폼 (GET ?email=...&name=... 으로 등록 가능)
  const email = e && e.parameter && e.parameter.email;
  if (email) {
    addSubscriber_({ email: email, name: e.parameter.name || '', markets: e.parameter.markets || 'kr,us,futures' });
    return _json({ ok: true, message: '구독 완료: ' + email });
  }
  const subs = getActiveSubscribers_();
  return _json({ ok: true, hint: 'POST picks payload here.', subscribers_count: subs.length });
}

// ---- setup: 처음 1회 실행 ------------------------------------------------

function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ensureSubscribersSheet_(ss);
  ensurePicksSheet_(ss);
  // 관리자 이메일이 비어 있으면 자동 등록
  const subs = ss.getSheetByName('Subscribers');
  if (subs.getLastRow() < 2 && ADMIN_EMAIL) {
    subs.appendRow([ADMIN_EMAIL, '관리자', true, new Date(), 'kr,us,futures', '관리자 자동 등록']);
  }
  Logger.log('setup 완료. Subscribers 시트에 이메일을 행 단위로 추가하세요.');
}

// ---- Subscribers 시트 ----------------------------------------------------

function ensureSubscribersSheet_(ss) {
  let sheet = ss.getSheetByName('Subscribers');
  if (!sheet) {
    sheet = ss.insertSheet('Subscribers', 0);
    sheet.appendRow(['email', 'name', 'active', 'subscribed_at', 'markets', 'memo']);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, 6).setFontWeight('bold').setBackground('#E0E7FF');
    sheet.setColumnWidth(1, 240);
    sheet.setColumnWidth(2, 120);
    sheet.setColumnWidth(3, 70);
    sheet.setColumnWidth(4, 160);
    sheet.setColumnWidth(5, 120);
    sheet.setColumnWidth(6, 200);
    // active 컬럼 체크박스 + markets 콤보박스
    sheet.getRange('C2:C').insertCheckboxes();
    const rule = SpreadsheetApp.newDataValidation()
      .requireValueInList(['kr,us,futures', 'kr,us', 'kr', 'us', 'us,futures'], true)
      .build();
    sheet.getRange('E2:E').setDataValidation(rule);
  }
  return sheet;
}

function getActiveSubscribers_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ensureSubscribersSheet_(ss);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const rows = sheet.getRange(2, 1, lastRow - 1, 6).getValues();
  return rows
    .filter(function(r) { return r[0] && r[2] !== false && String(r[2]).toLowerCase() !== 'false'; })
    .map(function(r) {
      return {
        email: String(r[0]).trim(),
        name: String(r[1] || '').trim(),
        markets: String(r[4] || 'kr,us,futures').split(',').map(function(s){return s.trim();}).filter(Boolean),
      };
    });
}

function addSubscriber_(payload) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ensureSubscribersSheet_(ss);
  const email = String(payload.email || '').trim();
  if (!email || email.indexOf('@') < 0) throw new Error('invalid email: ' + email);

  // 중복 검사
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    const existing = sheet.getRange(2, 1, lastRow - 1, 1).getValues().flat()
      .map(function(v){return String(v).trim().toLowerCase();});
    if (existing.indexOf(email.toLowerCase()) >= 0) {
      return { existed: true, email: email };
    }
  }
  sheet.appendRow([
    email,
    String(payload.name || '').trim(),
    true,
    new Date(),
    String(payload.markets || 'kr,us,futures'),
    String(payload.memo || ''),
  ]);
  return { existed: false, email: email };
}

// ---- Picks 시트 누적 ----------------------------------------------------

function ensurePicksSheet_(ss) {
  let sheet = ss.getSheetByName('Picks');
  if (!sheet) {
    sheet = ss.insertSheet('Picks');
    sheet.appendRow([
      'timestamp_kst', 'market', 'ticker', 'name', 'sector',
      'price', 'change_1d_pct', 'rsi', 'score',
      'entry_low', 'entry_high', 'target', 'stoploss',
      'one_liner', 'site_url',
    ]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, 15).setFontWeight('bold').setBackground('#E0E7FF');
  }
  return sheet;
}

function appendToPicks_(data) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ensurePicksSheet_(ss);
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

// ---- 다중 구독자 이메일 발송 ---------------------------------------------

function sendEmailsToAll_(data) {
  const subs = getActiveSubscribers_();
  if (subs.length === 0) {
    Logger.log('활성 구독자 없음 — 이메일 스킵');
    return 0;
  }

  const subject = '[데일리 픽] ' + (data.updated_at_kst || '') + ' 종목 갱신';
  let sent = 0;
  let failed = 0;

  for (const sub of subs) {
    try {
      // 구독자가 원하는 시장만 포함된 데이터로 필터링
      const personalized = filterByMarkets_(data, sub.markets);
      const html = buildEmailHtml_(personalized, sub.name);
      const text = buildEmailText_(personalized, sub.name);
      MailApp.sendEmail({
        to: sub.email,
        subject: subject,
        body: text,
        htmlBody: html,
        name: '데일리 픽',
      });
      sent++;
    } catch (err) {
      Logger.log('발송 실패 ' + sub.email + ': ' + err);
      failed++;
    }
  }
  Logger.log('이메일 발송 결과: 성공 ' + sent + '건 / 실패 ' + failed + '건 / 전체 ' + subs.length + '건');
  return sent;
}

function filterByMarkets_(data, markets) {
  // markets 가 ['kr','us','futures'] 같은 배열. 미포함 시장은 picks 비우기.
  const out = Object.assign({}, data);
  const all = ['kr', 'us', 'futures'];
  for (const m of all) {
    if (markets.indexOf(m) < 0 && out[m]) {
      out[m] = Object.assign({}, out[m], { picks: [] });
    }
  }
  return out;
}

// ---- 이메일 HTML / 텍스트 ------------------------------------------------

function buildEmailHtml_(data, recipientName) {
  const greeting = recipientName ? recipientName + '님, ' : '';
  const fear = data.fear || {};
  const fearRow = function(label, info) { return info
    ? '<tr><td style="padding:4px 12px;color:#6B7280">' + label + '</td>' +
       '<td style="padding:4px 12px;font-weight:600">' + info.value + ' ' + info.light + ' ' + info.label + '</td>' +
       '<td style="padding:4px 12px;color:#6B7280">' + (info.summary || '') + '</td></tr>'
    : ''; };

  let html = '' +
'<div style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0B1B3D">' +
'  <h2 style="margin:0 0 4px 0">데일리 픽 — 오늘의 종목</h2>' +
'  <div style="color:#6B7280;font-size:13px">' + greeting + '갱신: ' + (data.updated_at_kst || '') + '</div>' +
'  <table style="border-collapse:collapse;margin:16px 0;font-size:13px">' +
     fearRow('🇰🇷 한국장 공포지수', fear.vkospi) +
     fearRow('🇺🇸 미장 공포지수', fear.vix) +
'  </table>';

  for (const [market, label] of [['kr', '🇰🇷 코스피·코스닥'], ['us', '🇺🇸 나스닥·NYSE'], ['futures', '📊 선물·ETF']]) {
    const picks = (data[market] || {}).picks || [];
    if (!picks.length) continue;
    html += '<h3 style="margin:24px 0 8px 0;border-bottom:2px solid #1E3A8A;padding-bottom:6px">' + label + '</h3>';
    for (const p of picks) {
      const cur = market === 'us' ? '$' : '';
      const priceFmt = market === 'us'
        ? '$' + (p.price || 0).toFixed(2) + ' (≈ ' + (p.price_krw || 0).toLocaleString() + '원)'
        : (p.price || 0).toLocaleString() + '원';
      html += '' +
'<div style="border:1px solid #EAEAF0;border-radius:12px;padding:14px;margin-bottom:10px">' +
'  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">' +
'    <div>' +
'      <span style="background:#F1F5F9;color:#334155;padding:2px 8px;border-radius:999px;font-size:11px">' + (p.sector || market) + '</span>' +
'      <strong style="font-size:16px;margin-left:6px">' + p.ticker + '</strong>' +
'      <span style="color:#6B7280;font-size:13px"> ' + (p.name || '').substring(0, 25) + '</span>' +
'    </div>' +
'    <span style="background:#DBEAFE;color:#1E40AF;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600">' + (p.score || 0).toFixed(0) + '점</span>' +
'  </div>' +
'  <div style="margin-top:6px;font-size:18px;font-weight:700">' + priceFmt + '</div>' +
'  <div style="margin-top:6px;font-size:13px;color:#374151">' + ((p.narrative && p.narrative.one_liner) || ((p.reasons || []).slice(0, 2).join(', '))) + '</div>' +
'  <table style="width:100%;margin-top:8px;font-size:12px;border-collapse:collapse">' +
'    <tr>' +
'      <td style="background:#F1F5F9;padding:8px;text-align:center;border-radius:6px;width:33%">📥 진입<br/><strong>' + cur + (p.entry_low || 0).toLocaleString() + '~' + cur + (p.entry_high || 0).toLocaleString() + '</strong></td>' +
'      <td style="background:#FEE2E2;padding:8px;text-align:center;border-radius:6px;color:#991B1B;width:33%">🛑 손절<br/><strong>' + cur + (p.stoploss || 0).toLocaleString() + '</strong></td>' +
'      <td style="background:#DCFCE7;padding:8px;text-align:center;border-radius:6px;color:#166534;width:33%">🎯 목표<br/><strong>' + cur + (p.target || 0).toLocaleString() + '</strong></td>' +
'    </tr>' +
'  </table>' +
'</div>';
    }
  }

  html += '' +
'  <div style="margin-top:24px">' +
'    <a href="' + SITE_URL + '" style="display:inline-block;background:#0B1B3D;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">전체 보기 →</a>' +
'  </div>' +
'  <p style="margin-top:24px;font-size:11px;color:#9A3412;background:#FFF7ED;border:1px solid #FED7AA;padding:10px;border-radius:8px;line-height:1.6">' +
'    본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.' +
'    과거 수익률은 미래 수익을 보장하지 않습니다. 본 서비스는 「자본시장법」상 유사투자자문업으로 신고된 1:多 일방 발송 정보 서비스입니다.' +
'  </p>' +
'  <p style="margin-top:8px;font-size:11px;color:#9CA3AF">' +
'    수신 거부를 원하시면 회신 주시거나, 관리자에게 알려주세요.' +
'  </p>' +
'</div>';
  return html;
}

function buildEmailText_(data, recipientName) {
  const greeting = recipientName ? recipientName + '님, ' : '';
  let txt = greeting + '데일리 픽 종목 갱신\n갱신: ' + (data.updated_at_kst || '') + '\n\n';
  const fear = data.fear || {};
  if (fear.vkospi) txt += '🇰🇷 VKOSPI: ' + fear.vkospi.value + ' ' + fear.vkospi.label + '\n';
  if (fear.vix)    txt += '🇺🇸 VIX:    ' + fear.vix.value + ' ' + fear.vix.label + '\n';
  txt += '\n';
  for (const [m, label] of [['kr', '🇰🇷 코스피·코스닥'], ['us', '🇺🇸 나스닥·NYSE'], ['futures', '📊 선물·ETF']]) {
    const picks = (data[m] || {}).picks || [];
    if (!picks.length) continue;
    txt += '=== ' + label + ' ===\n';
    for (const p of picks) {
      const cur = m === 'us' ? '$' : '';
      txt += '  · ' + p.ticker + ' ' + (p.name || '') + ' (' + (p.sector || m) + ')\n';
      txt += '    가격: ' + cur + (p.price || 0).toLocaleString() + (m === 'us' ? '' : '원') + '  |  점수: ' + (p.score || 0).toFixed(0) + '점\n';
      if (p.entry_low) txt += '    진입 ' + cur + p.entry_low + '~' + cur + p.entry_high + ' / 손절 ' + cur + p.stoploss + ' / 목표 ' + cur + p.target + '\n';
      const oneLiner = (p.narrative && p.narrative.one_liner) || '';
      if (oneLiner) txt += '    ' + oneLiner + '\n';
    }
    txt += '\n';
  }
  txt += '전체 보기: ' + SITE_URL + '\n\n';
  txt += '본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.\n';
  txt += '과거 수익률은 미래 수익을 보장하지 않습니다.\n';
  return txt;
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
