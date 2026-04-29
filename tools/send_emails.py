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


def build_html(data: dict, name: str = "") -> str:
    greeting = f"{name}님, " if name else ""
    fear = data.get("fear", {}) or {}

    def fear_row(label, info):
        if not info: return ""
        return (f'<tr><td style="padding:4px 12px;color:#6B7280">{label}</td>'
                f'<td style="padding:4px 12px;font-weight:600">{info.get("value")} {info.get("light","")} {info.get("label","")}</td>'
                f'<td style="padding:4px 12px;color:#6B7280">{info.get("summary","")}</td></tr>')

    html = [
        '<div style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0B1B3D">',
        '  <h2 style="margin:0 0 4px 0">데일리 픽 — 오늘의 종목</h2>',
        f'  <div style="color:#6B7280;font-size:13px">{greeting}갱신: {data.get("updated_at_kst","")}</div>',
        '  <table style="border-collapse:collapse;margin:16px 0;font-size:13px">',
        fear_row("🇰🇷 한국장 공포지수", fear.get("vkospi")),
        fear_row("🇺🇸 미장 공포지수", fear.get("vix")),
        '  </table>',
    ]

    for market, label in [("kr", "🇰🇷 코스피·코스닥"), ("us", "🇺🇸 나스닥·NYSE"), ("futures", "📊 선물·ETF")]:
        picks = ((data.get(market) or {}).get("picks")) or []
        if not picks: continue
        html.append(f'<h3 style="margin:24px 0 8px 0;border-bottom:2px solid #1E3A8A;padding-bottom:6px">{label}</h3>')
        for p in picks:
            cur = "$" if market == "us" else ""
            try:
                price = float(p.get("price") or 0)
                if market == "us":
                    krw = int(p.get("price_krw") or 0)
                    price_fmt = f"${price:,.2f} (≈ {krw:,}원)"
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

            html.append(f'''
<div style="border:1px solid #EAEAF0;border-radius:12px;padding:14px;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div>
      <span style="background:#F1F5F9;color:#334155;padding:2px 8px;border-radius:999px;font-size:11px">{sector}</span>
      <strong style="font-size:16px;margin-left:6px">{ticker}</strong>
      <span style="color:#6B7280;font-size:13px"> {cname}</span>
    </div>
    <span style="background:#DBEAFE;color:#1E40AF;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600">{score_str}점</span>
  </div>
  <div style="margin-top:6px;font-size:18px;font-weight:700">{price_fmt}</div>
  <div style="margin-top:6px;font-size:13px;color:#374151">{one_liner}</div>
  <table style="width:100%;margin-top:8px;font-size:12px;border-collapse:collapse">
    <tr>
      <td style="background:#F1F5F9;padding:8px;text-align:center;border-radius:6px;width:33%">📥 진입<br/><strong>{elo}~{ehi}</strong></td>
      <td style="background:#FEE2E2;padding:8px;text-align:center;border-radius:6px;color:#991B1B;width:33%">🛑 손절<br/><strong>{stp}</strong></td>
      <td style="background:#DCFCE7;padding:8px;text-align:center;border-radius:6px;color:#166534;width:33%">🎯 목표<br/><strong>{tgt}</strong></td>
    </tr>
  </table>
</div>''')

    html.append(f'''
  <div style="margin-top:24px">
    <a href="{SITE_URL}" style="display:inline-block;background:#0B1B3D;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">전체 보기 →</a>
  </div>
  <p style="margin-top:24px;font-size:11px;color:#9A3412;background:#FFF7ED;border:1px solid #FED7AA;padding:10px;border-radius:8px;line-height:1.6">
    본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다.
    과거 수익률은 미래 수익을 보장하지 않습니다. 본 서비스는 「자본시장법」상 유사투자자문업으로 신고된 1:多 일방 발송 정보 서비스입니다.
  </p>
  <p style="margin-top:8px;font-size:11px;color:#9CA3AF">
    수신 거부를 원하시면 회신 주시거나 관리자에게 알려주세요.
  </p>
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

    csv_url = os.getenv("SUBSCRIBERS_SHEET_CSV", "").strip()
    if not csv_url:
        log.warning("SUBSCRIBERS_SHEET_CSV 미설정 — 스킵 (이메일 발송 안 함)")
        return 0

    subs = fetch_subscribers(csv_url)
    if not subs:
        log.warning("활성 구독자 0명 — 발송 스킵")
        return 0
    log.info("구독자 %d명 fetch", len(subs))

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pw   = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    dry = os.getenv("NOTIFY_DRY_RUN", "").lower() in ("1", "true", "yes")
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
