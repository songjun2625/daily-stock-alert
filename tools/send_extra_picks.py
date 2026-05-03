"""
1회성 보너스 발송 — 오늘 메인 추천 (4+1) 에 들지 않은 '추가 후보군' 안내.

흐름:
  1) screen_kr / screen_us 를 큰 N (15개) 로 실행
  2) picks.json 의 기존 추천 (4 기술 + 1 퀀트) 제외
  3) 점수 임계 통과한 다음 5+5 종목을 별도 이메일로 발송

용도:
  - "왜 이 종목은 안 추천했지?" 사용자 호기심 해소
  - Standard/Pro 플랜 업셀 (다음 단계 알고리즘 노출)
  - 알고리즘 투명성 — 같은 데이터 본 결과를 모두 공개

환경변수:
  GMAIL_USER, GMAIL_APP_PASSWORD : 발신
  TEST_RECIPIENT_EMAIL          : 단일 수신자 (필수 — 안전장치)

사용:
  TEST_RECIPIENT_EMAIL=user@example.com python tools/send_extra_picks.py
"""
from __future__ import annotations
import os, sys, json, ssl, smtplib, logging
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener.config import KST, QUALITY
from screener.screener_kr import screen_kr
from screener.screener_us import screen_us
from screener.publish import _apply_runtime_overrides, _load_runtime
from screener import narrative

log = logging.getLogger("send_extra")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parent.parent
PICKS_PATH = ROOT / "landing" / "data" / "picks.json"
SITE_URL = "https://songjun2625.github.io/daily-stock-alert/today.html"

VERDICT_MAP = [
    (130, "🟢", "강력 매수", "#D1FAE5", "#065F46", "#10B981"),
    (100, "🟢", "매수",     "#DCFCE7", "#166534", "#22C55E"),
    (80,  "🟡", "신중 매수", "#FEF3C7", "#92400E", "#F59E0B"),
    (60,  "🟡", "관망",     "#FEF9C3", "#854D0E", "#FACC15"),
    (0,   "🔴", "미추천",   "#F3F4F6", "#374151", "#9CA3AF"),
]


def verdict(score: float) -> dict:
    s = float(score or 0)
    for thresh, emoji, label, bg, color, border in VERDICT_MAP:
        if s >= thresh:
            return {"emoji": emoji, "label": label, "bg": bg, "color": color, "border": border}
    return {"emoji": "🔴", "label": "미추천", "bg": "#F3F4F6", "color": "#374151", "border": "#9CA3AF"}


def existing_tickers() -> tuple[set, set]:
    """picks.json 의 오늘 추천 종목 — 중복 발송 방지."""
    if not PICKS_PATH.exists():
        return set(), set()
    d = json.loads(PICKS_PATH.read_text(encoding="utf-8"))
    kr = {p.get("ticker") for p in (d.get("kr", {}).get("picks") or [])}
    if d.get("kr", {}).get("quant_pick"):
        kr.add(d["kr"]["quant_pick"].get("ticker"))
    us = {p.get("ticker") for p in (d.get("us", {}).get("picks") or [])}
    if d.get("us", {}).get("quant_pick"):
        us.add(d["us"]["quant_pick"].get("ticker"))
    return kr, us


def candidate_card(c, market: str, rank: int) -> str:
    v = verdict(c.score)
    is_us = market == "us"
    if is_us:
        price_txt = f"${c.price:,.2f}"
        krw = getattr(c, "price_krw", 0) or 0
        krw_html = f' <span style="font-size:11px;color:#6B7280">≈ {krw:,}원</span>' if krw else ''
        fmt = lambda x: f"${(x or 0):.2f}"
    else:
        price_txt = f"{int(c.price):,}원"
        krw_html = ''
        fmt = lambda x: f"{int(x or 0):,}원"
    pct = max(0, min(100, c.score / 150 * 100))
    fill = v["border"]
    sector = getattr(c, "sector", "") or ""
    om = (c.operating_margin or 0) * 100 if c.operating_margin else 0
    rg = (c.revenue_growth or 0) * 100 if c.revenue_growth else 0
    om_str = f"{om:.0f}%" if om else "—"
    rg_str = f"{rg:.0f}%" if rg else "—"
    one_liner = ""
    try:
        narr = narrative.narrate_us(c) if is_us else narrative.narrate_kr(c)
        one_liner = narr.get("one_liner") or ""
    except Exception:
        pass
    return f'''
<div style="border:1px solid #EAEAF0;border-radius:12px;padding:12px;margin-bottom:10px">
  <div style="font-size:14px;font-weight:700;line-height:1.3">
    <span style="display:inline-block;width:22px;height:22px;border-radius:50%;background:#94A3B8;color:#fff;font-weight:800;font-size:11px;text-align:center;line-height:22px;margin-right:6px;vertical-align:middle">+{rank}</span>
    {c.ticker} <span style="color:#6B7280;font-weight:500;font-size:12px">{c.name}</span>
  </div>
  <div style="margin-top:4px;font-size:16px;font-weight:800;color:#0B1B3D">{price_txt}{krw_html}</div>
  <div style="margin-top:6px">
    <span style="background:#F1F5F9;color:#334155;padding:2px 7px;border-radius:999px;font-size:10px;margin-right:4px">{sector}</span>
    <span style="background:{v['bg']};color:{v['color']};border:1px solid {v['border']};padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700">{v['emoji']} {v['label']}</span>
    <span style="color:#374151;font-size:10px;margin-left:4px">점수 {c.score:.0f} / 150</span>
  </div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin-top:6px">
    <tr>
      <td style="width:{pct:.1f}%;height:5px;background:{fill};border-radius:3px 0 0 3px;font-size:0;line-height:0">&nbsp;</td>
      <td style="width:{100-pct:.1f}%;height:5px;background:#E5E7EB;border-radius:0 3px 3px 0;font-size:0;line-height:0">&nbsp;</td>
    </tr>
  </table>
  {f'<div style="margin-top:8px;font-size:12px;color:#374151;line-height:1.5">{one_liner}</div>' if one_liner else ''}
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-top:8px;border-collapse:collapse;table-layout:fixed">
    <tr>
      <td style="background:#F8FAFC;border-radius:5px;padding:5px 7px;width:33%">
        <div style="color:#6B7280;font-size:9px">영업이익률</div>
        <div style="font-weight:700;font-size:11px">{om_str}</div>
      </td>
      <td style="width:1%"></td>
      <td style="background:#F8FAFC;border-radius:5px;padding:5px 7px;width:33%">
        <div style="color:#6B7280;font-size:9px">매출 성장</div>
        <div style="font-weight:700;font-size:11px">{rg_str}</div>
      </td>
      <td style="width:1%"></td>
      <td style="background:#F8FAFC;border-radius:5px;padding:5px 7px;width:32%">
        <div style="color:#6B7280;font-size:9px">RSI</div>
        <div style="font-weight:700;font-size:11px">{c.rsi:.0f}</div>
      </td>
    </tr>
  </table>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-top:6px;font-size:11px;border-collapse:separate;border-spacing:0 3px">
    <tr><td style="background:#F1F5F9;padding:6px 9px;border-radius:5px;font-weight:600;color:#334155;width:60px">📥 매수가</td><td style="background:#F1F5F9;padding:6px 9px;border-radius:5px;font-weight:700;text-align:right">{fmt(c.entry_low)} ~ {fmt(c.entry_high)}</td></tr>
    <tr><td style="background:#FEE2E2;padding:6px 9px;border-radius:5px;font-weight:600;color:#991B1B;width:60px">🛑 손절가</td><td style="background:#FEE2E2;padding:6px 9px;border-radius:5px;font-weight:700;color:#991B1B;text-align:right">{fmt(c.stoploss)}</td></tr>
    <tr><td style="background:#DCFCE7;padding:6px 9px;border-radius:5px;font-weight:600;color:#166534;width:60px">🎯 목표가</td><td style="background:#DCFCE7;padding:6px 9px;border-radius:5px;font-weight:700;color:#166534;text-align:right">{fmt(c.target)}</td></tr>
  </table>
</div>'''


def build_email_html(kr_extras: list, us_extras: list) -> str:
    parts = [
        '<div style="font-family:-apple-system,system-ui,sans-serif;max-width:680px;margin:0 auto;color:#0B1B3D;background:#F7F8FB;padding:14px">',
        '<div style="background:#fff;border-radius:14px;padding:18px;border:1px solid #EAEAF0">',
        '<div style="margin-bottom:6px">',
        '  <span style="display:inline-block;width:20px;height:20px;border-radius:5px;background:linear-gradient(135deg,#7C3AED,#312E81);vertical-align:middle"></span>',
        '  <span style="font-weight:800;font-size:13px;color:#0B1B3D;letter-spacing:.3px;vertical-align:middle;margin-left:6px">데일리 픽 · 추가 후보군</span>',
        '</div>',
        '<h2 style="margin:0 0 4px 0;font-size:18px">📂 오늘 본 추천 종목 외 추가 후보군</h2>',
        '<div style="color:#6B7280;font-size:11px;line-height:1.6">메인 추천 (4 기술 + 1 퀀트) 에 들지 못했지만 임계 점수 통과한 다음 후보군입니다. 알고리즘 투명성 차원에서 1회성 안내 발송.</div>',
        '<div style="background:#FEF3C7;border:1px solid #FDE68A;border-radius:8px;padding:10px 12px;margin-top:12px;font-size:11px;color:#92400E;line-height:1.6">',
        '<b>⚠️ 활용 시 주의</b><br/>',
        '- 메인 추천보다 <b>점수가 낮아</b> 진입 신호 강도가 약합니다<br/>',
        '- 포지션 사이즈는 메인 추천의 <b>50% 이내</b>로 보수적으로<br/>',
        '- 손절가 도달 시 즉시 매도 — 메인보다 더 엄격하게',
        '</div>',
    ]
    if kr_extras:
        parts.append('<h3 style="margin:18px 0 8px 0;font-size:14px;border-bottom:2px solid #7C3AED;padding-bottom:5px">🇰🇷 코스피·코스닥 추가 후보 <span style="font-size:11px;font-weight:500;color:#6B7280">— 점수 순위 5위~</span></h3>')
        for i, c in enumerate(kr_extras, 5):
            parts.append(candidate_card(c, "kr", i))
    if us_extras:
        parts.append('<h3 style="margin:18px 0 8px 0;font-size:14px;border-bottom:2px solid #7C3AED;padding-bottom:5px">🇺🇸 나스닥·NYSE 추가 후보 <span style="font-size:11px;font-weight:500;color:#6B7280">— 점수 순위 5위~</span></h3>')
        for i, c in enumerate(us_extras, 5):
            parts.append(candidate_card(c, "us", i))
    if not kr_extras and not us_extras:
        parts.append('<div style="text-align:center;padding:30px 12px;color:#9CA3AF;font-size:12px">오늘은 임계 점수 통과한 추가 후보가 없습니다 — 메인 추천 외 진입을 자제합니다.</div>')

    parts.append(f'''
  <div style="margin-top:18px">
    <a href="{SITE_URL}" style="display:block;background:#0B1B3D;color:#fff;padding:11px 16px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;text-align:center">📈 라이브 트래킹 페이지 →</a>
  </div>
  <p style="margin-top:18px;font-size:10px;color:#9A3412;background:#FFF7ED;border:1px solid #FED7AA;padding:10px;border-radius:6px;line-height:1.6">
    <b>⚠️ 면책</b> · 본 정보는 투자 권유가 아니며, 모든 투자 결과는 투자자 본인에게 귀속됩니다. 추가 후보군은 메인 추천보다 신호 강도가 약하므로 더 신중한 진입을 권장합니다.
  </p>
</div>
<div style="text-align:center;padding:14px 6px;font-size:10px;color:#9CA3AF;line-height:1.6">
  © 2026 PortZone Inc. · (주)포트존 · 유사투자자문업 신고: 제○○○○-○○○호
</div>
</div>''')
    return "\n".join(parts)


def main():
    test_email = (os.getenv("TEST_RECIPIENT_EMAIL") or "").strip()
    if not test_email or "@" not in test_email:
        log.error("TEST_RECIPIENT_EMAIL 환경변수 필수 (안전장치) — 단일 수신자 메일만 보냅니다")
        sys.exit(1)

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pw   = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not (gmail_user and gmail_pw):
        log.error("GMAIL_USER / GMAIL_APP_PASSWORD 미설정"); sys.exit(2)

    rt = _load_runtime()
    log.info("📊 KR 스크리너 실행 (top 15)")
    kr_all = screen_kr(top_n=15)
    log.info("📊 US 스크리너 실행 (top 15)")
    us_all = screen_us(top_n=15)

    # 런타임 override 적용
    kr_all, us_all = _apply_runtime_overrides(kr_all, us_all, rt)

    # 점수 임계 적용
    min_kr = rt.get("min_score_kr_override") or QUALITY.min_score_kr
    min_us = rt.get("min_score_us_override") or QUALITY.min_score_us
    kr_all = [c for c in kr_all if (c.score or 0) >= min_kr]
    us_all = [c for c in us_all if (c.score or 0) >= min_us]

    # 기존 추천 제외
    kr_existing, us_existing = existing_tickers()
    log.info("기존 추천 — KR %d, US %d", len(kr_existing), len(us_existing))
    kr_extras = [c for c in kr_all if c.ticker not in kr_existing][:5]
    us_extras = [c for c in us_all if c.ticker not in us_existing][:5]
    log.info("추가 후보 — KR %d개, US %d개", len(kr_extras), len(us_extras))

    html = build_email_html(kr_extras, us_extras)
    text = f"데일리 픽 — 추가 후보군 (KR {len(kr_extras)}, US {len(us_extras)}). 라이브 페이지: {SITE_URL}"

    msg = EmailMessage()
    msg["Subject"] = "[데일리 픽] 📂 오늘의 추가 후보군 (메인 추천 외)"
    msg["From"] = f"데일리 픽 <{gmail_user}>"
    msg["To"] = test_email
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(gmail_user, gmail_pw)
        smtp.send_message(msg)

    log.info("✅ 발송 완료: %s (KR %d + US %d)", test_email, len(kr_extras), len(us_extras))


if __name__ == "__main__":
    main()
