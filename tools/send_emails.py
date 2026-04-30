"""
picks.json 갱신을 구독자 전원에게 Gmail SMTP 로 발송 (Apps Script 불필요).

흐름:
  1. landing/data/picks.json 읽기
  2. SUBSCRIBERS_SHEET_CSV (공개 구글시트 CSV export URL) 에서 구독자 행 fetch
  3. 활성 구독자 + markets 필터 적용해 개별 이메일 생성
  4. Gmail SMTP (smtp.gmail.com:465) 로 발송

환경변수:
  GMAIL_USER             — 발신 Gmail 주소 (예: songjun2625@gmail.com)
  GMAIL_APP_PASSWORD     — Gmail 앱 비밀번호 (16자리, 공백 없음)
  SUBSCRIBERS_SHEET_CSV  — 'Subscribers' 시트의 공개 CSV URL
                            (https://docs.google.com/spreadsheets/d/.../export?format=csv&gid=0)
  PICKS_JSON             — picks.json 경로 (기본 landing/data/picks.json)
  NOTIFY_DRY_RUN         — '1' 이면 실제 발송 없이 콘솔에 미리보기

구독자 시트 컬럼 (1행은 헤더, 순서·이름은 자유 — 자동 매핑):
  email, name, active, markets, ...

  - email   : 필수
  - name    : 선택 (없어도 OK)
  - active  : TRUE/FALSE / 1/0 / '' (빈값 = TRUE)
  - markets : 'kr,us,futures' 중 콤마구분. 빈값이면 모두 받음.
"""
from __future__ import annotations
import os, sys, csv, ssl, json, smtplib, logging
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from typing import Iterable
from urllib import request
from urllib.error import URLError, HTTPError

log = logging.getLogger("send_emails")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

PICKS_PATH = Path(os.getenv("PICKS_JSON", "landing/data/picks.json"))
SITE_URL = "https://songjun2625.github.io/daily-stock-alert/today.html"


# ---- 구독자 fetch (공개 시트 CSV) ----------------------------------------

def fetch_subscribers(csv_url: str) -> list[dict]:
    """공개 구글시트 CSV export URL 을 GET 해 구독자 행 파싱."""
    try:
        with request.urlopen(csv_url, timeout=20) as resp:
            text = resp.read().decode("utf-8-sig")  # BOM 제거
    except (HTTPError, URLError) as e:
        log.error("구독자 시트 fetch 실패: %s", e)
        return []

    reader = csv.DictReader(StringIO(text))
    rows: list[dict] = []
    for row in reader:
        # 컬럼명 lowercase + strip 정규화
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        email = norm.get("email", "")
        if not email or "@" not in email:
            continue
        active_raw = norm.get("active", "").lower()
        active = active_raw in ("", "true", "1", "y", "yes", "checked")
        if not active:
            continue
        # 컬럼명이 'markets' 또는 'market' 둘 다 허용
        markets_raw = norm.get("markets") or norm.get("market") or ""
        markets = [m.strip() for m in markets_raw.split(",") if m.strip()] if markets_raw else ["kr", "us", "futures"]
        rows.append({
            "email": email,
            "name": norm.get("name", ""),
            "markets": markets,
        })
    return rows


# ---- 이메일 본문 빌더 ----------------------------------------------------

def _filter_by_markets(data: dict, markets: list[str]) -> dict:
    out = dict(data)
    for m in ("kr", "us", "futures"):
        if m not in markets and m in out and isinstance(out[m], dict):
            out[m] = {**out[m], "picks": []}
    return out


def _fmt_money(value, market: str) -> str:
    if market == "us":
        try: return f"${float(value):,.2f}"
        except (TypeError, ValueError): return str(value)
    try: return f"{int(float(value)):,}원"
    except (TypeError, ValueError): return str(value)


def _load_live_summary() -> dict:
    """live_trades.json 의 누적 성과 — 메일 헤더 KPI 용."""
    p = Path("landing/data/live_trades.json")
    if not p.exists(): return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {
            "summary": d.get("summary", {}),
            "period_start": d.get("period_start", ""),
            "period_end": d.get("period_end", ""),
        }
    except Exception:
        return {}


def build_html(data: dict, name: str = "") -> str:
    greeting = f"{name}님, " if name else ""
    fear = data.get("fear", {}) or {}
    live = _load_live_summary()
    summary = live.get("summary") or {}
    cum = summary.get("cum_return_pct", 0)
    win = summary.get("win_rate_pct", 0)
    n   = summary.get("total_trades", 0)
    cum_color = "#FCA5A5" if cum >= 0 else "#93C5FD"
    cum_sign  = "+" if cum >= 0 else ""
    period_str = f"{live.get('period_start', '')} ~ {live.get('period_end', '')}" if live.get("period_start") else ""

    kpi_block = ""
    if n > 0:
        # 모바일 가독성: 폰트 22→16px / 라벨 11→10px / nowrap 강제 / 라벨 단축 / 패딩 축소
        kpi_block = (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
                     f'style="width:100%;background:linear-gradient(135deg,#0B1B3D,#1E3A8A);'
                     f'border-radius:12px;margin:0 0 14px 0;color:#fff">'
                     f'<tr><td style="padding:14px 14px 12px 14px">'
                     f'<div style="font-size:10px;color:#A5B4FC;letter-spacing:.4px;text-transform:uppercase;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
                     f'📊 누적 성과 · {period_str}</div>'
                     f'<table style="width:100%;margin-top:8px;border-collapse:collapse;table-layout:fixed"><tr>'
                     f'<td style="width:40%;padding:0 4px 0 0;vertical-align:top">'
                     f'<div style="font-size:10px;color:#A5B4FC;white-space:nowrap">누적 수익률</div>'
                     f'<div style="font-size:18px;font-weight:800;color:{cum_color};line-height:1.15;white-space:nowrap">{cum_sign}{cum:.1f}%</div>'
                     f'</td>'
                     f'<td style="width:30%;padding:0 4px;vertical-align:top">'
                     f'<div style="font-size:10px;color:#A5B4FC;white-space:nowrap">승률</div>'
                     f'<div style="font-size:18px;font-weight:800;color:#fff;line-height:1.15;white-space:nowrap">{win:.1f}%</div>'
                     f'</td>'
                     f'<td style="width:30%;padding:0 0 0 4px;vertical-align:top">'
                     f'<div style="font-size:10px;color:#A5B4FC;white-space:nowrap">청산</div>'
                     f'<div style="font-size:18px;font-weight:800;color:#fff;line-height:1.15;white-space:nowrap">{n}건</div>'
                     f'</td></tr></table>'
                     f'</td></tr></table>')

    # 공포지수 — 한 줄 카드 형태로 변경 (table 셀 wrap 이슈 회피)
    def fear_card(label, info):
        if not info: return ""
        return (f'<div style="display:block;padding:8px 12px;background:#F8FAFC;border-radius:8px;margin-bottom:6px;font-size:12px;line-height:1.5">'
                f'<span style="color:#6B7280;white-space:nowrap">{label}</span>'
                f'&nbsp;&nbsp;<span style="font-weight:700;white-space:nowrap">{info.get("value")} {info.get("light","")} {info.get("label","")}</span>'
                f'&nbsp;<span style="color:#6B7280;font-size:11px">— {info.get("summary","")}</span>'
                f'</div>')

    html = [
        '<div style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0B1B3D;background:#F7F8FB;padding:14px">',
        '<div style="background:#fff;border-radius:14px;padding:18px;border:1px solid #EAEAF0">',
        '<div style="display:block;margin-bottom:6px">',
        '  <span style="display:inline-block;width:20px;height:20px;border-radius:5px;background:linear-gradient(135deg,#0B1B3D,#1E3A8A);vertical-align:middle"></span>',
        '  <span style="font-weight:800;font-size:13px;color:#0B1B3D;letter-spacing:.3px;vertical-align:middle;margin-left:6px">데일리 픽</span>',
        '</div>',
        '  <h2 style="margin:0 0 3px 0;font-size:18px">오늘의 종목</h2>',
        f'  <div style="color:#6B7280;font-size:11px">{greeting}갱신: {data.get("updated_at_kst","")}</div>',
        kpi_block,
        fear_card("🇰🇷 한국장 (VKOSPI)", fear.get("vkospi")),
        fear_card("🇺🇸 미장 (VIX)", fear.get("vix")),
    ]

    for market, label in [("kr", "🇰🇷 코스피·코스닥"), ("us", "🇺🇸 나스닥·NYSE"), ("futures", "📊 선물·ETF")]:
        picks = ((data.get(market) or {}).get("picks")) or []
        if not picks: continue
        html.append(f'<h3 style="margin:18px 0 8px 0;font-size:14px;border-bottom:2px solid #1E3A8A;padding-bottom:5px">{label}</h3>')
        for p in picks:
            cur = "$" if market == "us" else ""
            try:
                price = float(p.get("price") or 0)
                if market == "us":
                    krw = int(p.get("price_krw") or 0)
                    price_fmt = f"${price:,.2f} <span style=\"font-size:12px;color:#6B7280;font-weight:500\">≈ {krw:,}원</span>"
                else:
                    price_fmt = f"{int(price):,}원"
            except (TypeError, ValueError):
                price_fmt = str(p.get("price"))
            sector = p.get("sector") or market
            ticker = p.get("ticker", "")
            cname = (p.get("name") or "")[:25]
            score = p.get("score") or 0
            try: score_str = f"{float(score):.0f}"
            except (TypeError, ValueError): score_str = "0"
            one_liner = ((p.get("narrative") or {}).get("one_liner")
                         or ", ".join((p.get("reasons") or [])[:2]))
            elo = _fmt_money(p.get("entry_low"), market)
            ehi = _fmt_money(p.get("entry_high"), market)
            stp = _fmt_money(p.get("stoploss"), market)
            tgt = _fmt_money(p.get("target"), market)

            # 모바일 우선: 헤더 줄바꿈 시 깨짐 방지 — 한 줄에 sector + ticker, 다음 줄에 name + score
            html.append(f'''
<div style="border:1px solid #EAEAF0;border-radius:12px;padding:12px;margin-bottom:10px">
  <div style="font-size:11px;color:#334155;margin-bottom:4px">
    <span style="background:#F1F5F9;padding:2px 8px;border-radius:999px;white-space:nowrap">{sector}</span>
    <span style="background:#DBEAFE;color:#1E40AF;padding:2px 8px;border-radius:999px;font-weight:600;margin-left:4px;white-space:nowrap">{score_str}점</span>
  </div>
  <div style="font-size:14px;font-weight:700;line-height:1.3">
    {ticker} <span style="color:#6B7280;font-weight:500;font-size:12px">{cname}</span>
  </div>
  <div style="margin-top:4px;font-size:16px;font-weight:800;color:#0B1B3D">{price_fmt}</div>
  <div style="margin-top:6px;font-size:12px;color:#374151;line-height:1.5">{one_liner}</div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-top:8px;font-size:12px;border-collapse:separate;border-spacing:0 3px">
    <tr>
      <td style="background:#F1F5F9;padding:8px 10px;border-radius:6px;font-weight:600;color:#334155;white-space:nowrap;width:60px">📥 진입</td>
      <td style="background:#F1F5F9;padding:8px 10px;border-radius:6px;font-weight:700;text-align:right;white-space:nowrap;font-size:12px">{elo} ~ {ehi}</td>
    </tr>
    <tr>
      <td style="background:#FEE2E2;padding:8px 10px;border-radius:6px;font-weight:600;color:#991B1B;white-space:nowrap;width:60px">🛑 손절</td>
      <td style="background:#FEE2E2;padding:8px 10px;border-radius:6px;font-weight:700;color:#991B1B;text-align:right;white-space:nowrap">{stp}</td>
    </tr>
    <tr>
      <td style="background:#DCFCE7;padding:8px 10px;border-radius:6px;font-weight:600;color:#166534;white-space:nowrap;width:60px">🎯 목표</td>
      <td style="background:#DCFCE7;padding:8px 10px;border-radius:6px;font-weight:700;color:#166534;text-align:right;white-space:nowrap">{tgt}</td>
    </tr>
  </table>
</div>''')

    html.append(f'''
  <div style="margin-top:18px">
    <a href="{SITE_URL}" style="display:block;background:#0B1B3D;color:#fff;padding:11px 16px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;text-align:center;margin-bottom:6px">📈 라이브 트래킹 페이지 보기 →</a>
    <a href="{SITE_URL.replace('today.html','index.html#pricing')}" style="display:block;background:#fff;color:#0B1B3D;border:1px solid #E5E7EB;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600;font-size:12px;text-align:center">요금제 보기</a>
  </div>
  <p style="margin-top:18px;font-size:10px;color:#9A3412;background:#FFF7ED;border:1px solid #FED7AA;padding:10px;border-radius:6px;line-height:1.6">
    <b>⚠️ 면책 및 고지</b><br/>
    본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다. 과거 수익률은 미래 수익을 보장하지 않습니다. 본 서비스는 「자본시장법」상 유사투자자문업으로 신고된 1:多 일방 발송 정보 서비스이며, 회원별 1:1 자문이나 자산·포트폴리오 분석은 제공하지 않습니다. 레버리지 ETF·선물·옵션은 손실이 원금의 2~3배로 확대될 수 있습니다.
  </p>
</div>
<div style="text-align:center;padding:14px 6px;font-size:10px;color:#9CA3AF;line-height:1.6">
  © 2026 PortZone Inc. · (주)포트존 · 유사투자자문업 신고: 제○○○○-○○○호<br/>
  문의 <a href="mailto:contact@portzone.kr" style="color:#6B7280">contact@portzone.kr</a> · 수신거부 080-***-****
</div>
</div>''')
    return "\n".join(html)


def build_text(data: dict, name: str = "") -> str:
    greeting = f"{name}님, " if name else ""
    lines = [f"{greeting}데일리 픽 종목 갱신",
             f"갱신: {data.get('updated_at_kst', '')}", ""]
    fear = data.get("fear", {}) or {}
    if fear.get("vkospi"): lines.append(f"🇰🇷 VKOSPI: {fear['vkospi'].get('value')} {fear['vkospi'].get('label','')}")
    if fear.get("vix"):    lines.append(f"🇺🇸 VIX:    {fear['vix'].get('value')} {fear['vix'].get('label','')}")
    lines.append("")
    for market, label in [("kr", "🇰🇷 코스피·코스닥"), ("us", "🇺🇸 나스닥·NYSE"), ("futures", "📊 선물·ETF")]:
        picks = ((data.get(market) or {}).get("picks")) or []
        if not picks: continue
        lines.append(f"=== {label} ===")
        for p in picks:
            cur = "$" if market == "us" else ""
            lines.append(f"  · {p.get('ticker','')} {p.get('name','')} ({p.get('sector', market)})")
            price_fmt = _fmt_money(p.get("price"), market)
            lines.append(f"    가격: {price_fmt}  |  점수: {float(p.get('score') or 0):.0f}점")
            if p.get("entry_low"):
                lines.append(f"    진입 {cur}{p['entry_low']}~{cur}{p.get('entry_high','')} / 손절 {cur}{p.get('stoploss','')} / 목표 {cur}{p.get('target','')}")
            ol = ((p.get("narrative") or {}).get("one_liner") or "")
            if ol: lines.append(f"    {ol}")
        lines.append("")
    lines.append(f"전체 보기: {SITE_URL}")
    lines.append("")
    lines.append("본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.")
    lines.append("과거 수익률은 미래 수익을 보장하지 않습니다.")
    return "\n".join(lines)


# ---- SMTP ---------------------------------------------------------------

def send_email_gmail(to_email: str, subject: str, html_body: str, text_body: str,
                     gmail_user: str, gmail_password: str, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] would send to %s — len(html)=%d, subject=%s",
                 to_email, len(html_body), subject)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"데일리 픽 <{gmail_user}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)


# ---- main ---------------------------------------------------------------

def main() -> int:
    if not PICKS_PATH.exists():
        log.error("picks.json 없음: %s", PICKS_PATH); return 1

    data = json.loads(PICKS_PATH.read_text(encoding="utf-8"))

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pw   = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    dry = os.getenv("NOTIFY_DRY_RUN", "").lower() in ("1", "true", "yes")

    # 테스트 모드 — TEST_RECIPIENT_EMAIL 가 설정되면 시트 무시하고 그 한 명에게만 발송
    # (값이 비어있고 자동완성이 아니라면 GMAIL_USER 본인에게 발송)
    test_email = (os.getenv("TEST_RECIPIENT_EMAIL") or "").strip()
    if test_email:
        if "@" not in test_email:
            log.error("TEST_RECIPIENT_EMAIL 형식 오류: %s", test_email); return 4
        subs = [{"email": test_email, "name": "Test", "markets": ["kr", "us", "futures"]}]
        log.info("🧪 TEST 모드 — 단일 수신자 발송: %s", test_email)
    else:
        csv_url = os.getenv("SUBSCRIBERS_SHEET_CSV", "").strip()
        if not csv_url:
            log.warning("SUBSCRIBERS_SHEET_CSV 미설정 — 스킵 (이메일 발송 안 함)")
            return 0
        subs = fetch_subscribers(csv_url)
        if not subs:
            log.warning("활성 구독자 0명 — 발송 스킵")
            return 0
        log.info("구독자 %d명 fetch", len(subs))

    if not dry and not (gmail_user and gmail_pw):
        log.error("GMAIL_USER / GMAIL_APP_PASSWORD 미설정"); return 2

    subject = f"[데일리 픽] {data.get('updated_at_kst','')} 종목 갱신"
    sent = 0; failed = 0
    for sub in subs:
        personalized = _filter_by_markets(data, sub["markets"])
        try:
            send_email_gmail(
                to_email=sub["email"],
                subject=subject,
                html_body=build_html(personalized, sub["name"]),
                text_body=build_text(personalized, sub["name"]),
                gmail_user=gmail_user or "",
                gmail_password=gmail_pw or "",
                dry_run=dry,
            )
            sent += 1
        except Exception as e:
            log.error("발송 실패 %s: %s", sub["email"], e)
            failed += 1

    log.info("✅ 발송 완료: 성공 %d / 실패 %d / 전체 %d", sent, failed, len(subs))
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
