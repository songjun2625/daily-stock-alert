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


def _verdict(score: float) -> dict:
    """종목 점수 → 매수 의견 매핑 (today.html scoreVerdict 와 동일)."""
    s = float(score or 0)
    if s >= 130: return {"emoji": "🟢", "label": "강력 매수", "bg": "#D1FAE5", "color": "#065F46", "border": "#10B981",
                          "desc": "최상위 신호 다중 포착 — 펀더멘털·기술·섹터 모두 양호"}
    if s >= 100: return {"emoji": "🟢", "label": "매수",     "bg": "#DCFCE7", "color": "#166534", "border": "#22C55E",
                          "desc": "복수의 강한 신호 — 단기 진입 우호"}
    if s >= 80:  return {"emoji": "🟡", "label": "신중 매수", "bg": "#FEF3C7", "color": "#92400E", "border": "#F59E0B",
                          "desc": "기준 통과 — 분할매수 + 손절 엄수"}
    if s >= 60:  return {"emoji": "🟡", "label": "관망",     "bg": "#FEF9C3", "color": "#854D0E", "border": "#FACC15",
                          "desc": "약한 신호 — 다음 신호 확인 후 진입"}
    return                {"emoji": "🔴", "label": "미추천",  "bg": "#F3F4F6", "color": "#374151", "border": "#9CA3AF",
                          "desc": "임계 미달"}


def _score_block_html(score, sector: str = "") -> str:
    """이메일용 점수 게이지 — verdict 배지 + 0~150 progress bar.
    이메일 클라이언트는 calc() / position:absolute 가 자주 깨짐 → table 2-cell 구조로 교체."""
    v = _verdict(score)
    s = float(score or 0)
    pct = max(0, min(100, s / 150 * 100))
    filled_pct = pct
    empty_pct  = 100 - pct
    sector_pill = (f'<span style="background:#F1F5F9;color:#334155;padding:2px 7px;border-radius:999px;'
                   f'font-size:10px;white-space:nowrap;margin-right:4px">{sector}</span>') if sector else ""
    # Gmail/Outlook 호환 — table 2셀 progress bar + 별도 마커 레이어
    # 좌측 셀(채워진 영역): 점수 컬러 그라데이션 / 우측 셀(빈 영역): 회색
    # 마커는 좌측 셀의 우측 가장자리에 위치 — 추가 마커 라인을 하단에 한 개 더
    fill_color = v["border"]   # verdict 컬러 사용 (강력매수=진녹/매수=녹/신중=주황/관망=노랑/미추천=회색)
    return (
        f'<div style="margin-top:6px">'
        f'<div style="margin-bottom:4px">'
        f'  {sector_pill}'
        f'  <span style="background:{v["bg"]};color:{v["color"]};border:1px solid {v["border"]};'
        f'padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap">'
        f'{v["emoji"]} {v["label"]}</span>'
        f'  <span style="color:#374151;font-size:10px;margin-left:4px">점수 {s:.0f} / 150</span>'
        f'</div>'
        # 게이지 — table 기반 (이메일 호환)
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin-top:6px">'
        f'<tr>'
        f'<td style="width:{filled_pct:.1f}%;height:6px;background:{fill_color};border-radius:3px 0 0 3px;font-size:0;line-height:0">&nbsp;</td>'
        f'<td style="width:{empty_pct:.1f}%;height:6px;background:#E5E7EB;border-radius:0 3px 3px 0;font-size:0;line-height:0">&nbsp;</td>'
        f'</tr>'
        f'</table>'
        # 스케일 라벨
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin-top:3px;font-size:8px;color:#9CA3AF">'
        f'<tr>'
        f'<td style="text-align:left;width:20%">0</td>'
        f'<td style="text-align:center;width:20%">60 관망</td>'
        f'<td style="text-align:center;width:20%">80 신중</td>'
        f'<td style="text-align:center;width:20%">100 매수</td>'
        f'<td style="text-align:right;width:20%">130+ 강력</td>'
        f'</tr>'
        f'</table>'
        f'<div style="font-size:10px;color:#6B7280;margin-top:5px;line-height:1.5">{v["desc"]}</div>'
        f'</div>'
    )


def _market_quant_pick_html(qp: dict | None, market: str) -> str:
    """시장별 퀀트 픽 — 재무재표 기반. 시장 섹션 최상단에 노출."""
    if not qp or not qp.get("ticker"):
        return ""
    is_us = market == "us"
    flag = "🇺🇸" if is_us else "🇰🇷"
    price = qp.get("price") or 0
    if is_us:
        price_txt = f"${price:,.2f}"
        krw = qp.get("price_krw") or 0
        krw_html = f' <span style="font-size:10px;color:#A5B4FC;font-weight:500">≈ {krw:,}원</span>' if krw else ''
        fmt = lambda x: f"${(x or 0):.2f}"
    else:
        price_txt = f"{int(price):,}원"
        krw_html = ''
        fmt = lambda x: f"{int(x or 0):,}원"
    sector_html = (f'<span style="background:rgba(255,255,255,.15);color:#fff;'
                   f'padding:1px 7px;border-radius:999px;font-size:10px;margin-left:6px">{qp.get("sector","")}</span>'
                   ) if qp.get("sector") else ''
    # 펀더멘털 메트릭
    om = (qp.get("operating_margin") or 0) * 100
    rg = (qp.get("revenue_growth") or 0) * 100
    pe = qp.get("pe_ratio") or 0
    es_pct = qp.get("earnings_surprise_pct") if is_us else (qp.get("earnings_surprise") or 0)
    es = float(es_pct or 0)
    om_str = f"{om:.0f}%" if om else "—"
    rg_str = f"{rg:.0f}%" if rg else "—"
    pe_str = f"{pe:.1f}" if pe and pe > 0 else "—"
    es_str = f"{'+' if es >= 0 else ''}{es:.1f}%" if es != 0 else "—"
    qscore = qp.get("quant_score") or qp.get("score") or 0
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;'
        f'background:linear-gradient(135deg,#0B1B3D 0%,#312E81 50%,#7C3AED 100%);'
        f'border-radius:12px;margin:8px 0 8px 0;color:#fff">'
        f'<tr><td style="padding:12px 14px">'
        f'<div style="font-size:9px;font-weight:700;letter-spacing:.5px;color:#FFB020;text-transform:uppercase;margin-bottom:5px">'
        f'🌟 Quant Pick · 재무재표 기반</div>'
        f'<div style="font-size:14px;font-weight:800;line-height:1.3">'
        f'{flag} {qp.get("ticker","")} <span style="color:#E0E7FF;font-weight:500;font-size:12px">{qp.get("name","")}</span>'
        f'{sector_html}</div>'
        f'<div style="margin-top:5px">'
        f'<span style="font-size:18px;font-weight:800">{price_txt}</span>{krw_html}'
        f'&nbsp;<span style="background:rgba(255,255,255,.12);'
        f'padding:2px 7px;border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap">'
        f'퀀트 {qscore:.0f}점</span>'
        f'</div>'
        # 4 펀더멘털 메트릭
        f'<table style="width:100%;margin-top:7px;border-collapse:collapse;table-layout:fixed"><tr>'
        f'<td style="background:rgba(255,255,255,.08);border-radius:5px;padding:5px 7px;width:24%">'
        f'<div style="color:#A5B4FC;font-size:8px">영업이익률</div><div style="font-weight:700;font-size:11px">{om_str}</div></td>'
        f'<td style="width:1%"></td>'
        f'<td style="background:rgba(255,255,255,.08);border-radius:5px;padding:5px 7px;width:24%">'
        f'<div style="color:#A5B4FC;font-size:8px">매출 성장</div><div style="font-weight:700;font-size:11px">{rg_str}</div></td>'
        f'<td style="width:1%"></td>'
        f'<td style="background:rgba(255,255,255,.08);border-radius:5px;padding:5px 7px;width:24%">'
        f'<div style="color:#A5B4FC;font-size:8px">PER</div><div style="font-weight:700;font-size:11px">{pe_str}</div></td>'
        f'<td style="width:1%"></td>'
        f'<td style="background:rgba(255,255,255,.08);border-radius:5px;padding:5px 7px;width:25%">'
        f'<div style="color:#A5B4FC;font-size:8px">어닝</div><div style="font-weight:700;font-size:11px">{es_str}</div></td>'
        f'</tr></table>'
        # 진입/손절/목표
        f'<table style="width:100%;margin-top:6px;border-collapse:collapse;table-layout:fixed"><tr>'
        f'<td style="background:rgba(255,255,255,.10);border-radius:5px;padding:5px 7px;width:42%">'
        f'<div style="color:#A5B4FC;font-size:8px">📥 매수가</div>'
        f'<div style="font-weight:700;font-size:10px;white-space:nowrap">{fmt(qp.get("entry_low"))}~{fmt(qp.get("entry_high"))}</div></td>'
        f'<td style="width:1%"></td>'
        f'<td style="background:rgba(254,202,202,.18);border-radius:5px;padding:5px 7px;width:28%">'
        f'<div style="color:#FCA5A5;font-size:8px">🛑 손절가</div>'
        f'<div style="font-weight:700;font-size:10px;white-space:nowrap">{fmt(qp.get("stoploss"))}</div></td>'
        f'<td style="width:1%"></td>'
        f'<td style="background:rgba(167,243,208,.20);border-radius:5px;padding:5px 7px;width:28%">'
        f'<div style="color:#86EFAC;font-size:8px">🎯 목표가</div>'
        f'<div style="font-weight:700;font-size:10px;white-space:nowrap">{fmt(qp.get("target"))}</div></td>'
        f'</tr></table>'
        f'</td></tr></table>'
    )


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
        # 활용 가이드 — 초보자 4단계
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;background:linear-gradient(135deg,#FAFBFF,#F0F5FF);border:1px solid #DBEAFE;border-radius:10px;margin-top:10px">'
        '<tr><td style="padding:12px 14px">'
        '<div style="font-size:11px;font-weight:800;color:#0B1B3D;margin-bottom:8px">🎯 어떻게 활용하나요? — 4단계</div>'
        '<table style="width:100%;border-collapse:collapse;table-layout:fixed"><tr>'
        '<td style="width:25%;text-align:center;padding:0 4px">'
        '<div style="width:32px;height:32px;background:#1E3A8A;border-radius:50%;color:#fff;font-size:16px;line-height:32px;margin:0 auto">📨</div>'
        '<div style="font-size:9px;font-weight:700;color:#FFB020;margin-top:3px">STEP 1</div>'
        '<div style="font-size:10px;font-weight:700;color:#0B1B3D;margin-top:2px">메일 받기</div>'
        '<div style="font-size:9px;color:#6B7280;margin-top:1px;line-height:1.4">매일 아침 자동</div></td>'
        '<td style="width:25%;text-align:center;padding:0 4px">'
        '<div style="width:32px;height:32px;background:#3B82F6;border-radius:50%;color:#fff;font-size:16px;line-height:32px;margin:0 auto">🔍</div>'
        '<div style="font-size:9px;font-weight:700;color:#FFB020;margin-top:3px">STEP 2</div>'
        '<div style="font-size:10px;font-weight:700;color:#0B1B3D;margin-top:2px">등급 확인</div>'
        '<div style="font-size:9px;color:#6B7280;margin-top:1px;line-height:1.4">🟢 매수 / 🟡 신중</div></td>'
        '<td style="width:25%;text-align:center;padding:0 4px">'
        '<div style="width:32px;height:32px;background:#10B981;border-radius:50%;color:#fff;font-size:16px;line-height:32px;margin:0 auto">💰</div>'
        '<div style="font-size:9px;font-weight:700;color:#FFB020;margin-top:3px">STEP 3</div>'
        '<div style="font-size:10px;font-weight:700;color:#0B1B3D;margin-top:2px">분할 매수</div>'
        '<div style="font-size:9px;color:#6B7280;margin-top:1px;line-height:1.4">자본 10~20%</div></td>'
        '<td style="width:25%;text-align:center;padding:0 4px">'
        '<div style="width:32px;height:32px;background:#F59E0B;border-radius:50%;color:#fff;font-size:16px;line-height:32px;margin:0 auto">⏰</div>'
        '<div style="font-size:9px;font-weight:700;color:#FFB020;margin-top:3px">STEP 4</div>'
        '<div style="font-size:10px;font-weight:700;color:#0B1B3D;margin-top:2px">자동 청산</div>'
        '<div style="font-size:9px;color:#6B7280;margin-top:1px;line-height:1.4">5일 안에</div></td>'
        '</tr></table>'
        '<div style="font-size:10px;color:#475569;line-height:1.55;margin-top:10px;padding-top:8px;border-top:1px dashed #DBEAFE">'
        '<b>매수의견(verdict)</b> — 점수 5단계: '
        '<span style="background:#D1FAE5;color:#065F46;padding:1px 5px;border-radius:3px;font-size:9px">🟢 강력매수 130+</span> '
        '<span style="background:#DCFCE7;color:#166534;padding:1px 5px;border-radius:3px;font-size:9px">🟢 매수 100~130</span> '
        '<span style="background:#FEF3C7;color:#92400E;padding:1px 5px;border-radius:3px;font-size:9px">🟡 신중 80~100</span> '
        '<span style="background:#FEF9C3;color:#854D0E;padding:1px 5px;border-radius:3px;font-size:9px">🟡 관망 60~80</span>'
        '</div>'
        '<div style="font-size:10px;color:#475569;line-height:1.55;margin-top:6px">'
        '<b>3가지 원칙</b> · ✅ 손절가 무조건 지키기 · ✅ 한 종목 몰빵 금지 · ✅ 5일 안에 매도'
        '</div>'
        '</td></tr></table>',
    ]

    # 부족분 사유 결정
    runtime = data.get("runtime") or {}
    mode = (runtime.get("market_mode") or "normal").lower()
    vkospi = (fear.get("vkospi") or {}).get("value")
    vix = (fear.get("vix") or {}).get("value")
    def blank_reason(market: str) -> str:
        if mode == "crisis":    return "⚠️ 위기 모드 활성화 — 신규 추천 전면 보류 중"
        if mode == "defensive": return "🛡️ 방어 모드 — 추천 임계 +20점 상향, 통과 종목 부족"
        if market == "us" and vix and vix > 25:
            return f"미장 변동성 높음 (VIX {vix:.1f}) — 안전 진입대 미충족"
        if market == "kr" and vkospi and vkospi > 25:
            return f"한국장 변동성 높음 (VKOSPI {vkospi:.1f}) — 안전 진입대 미충족"
        if market == "us": return "SPY 50일선 strict regime + 임계 80점을 동시 통과하는 추가 후보 부족"
        if market == "kr": return "영업이익률·매출성장 + 임계 60점을 동시 통과하는 추가 후보 부족"
        return "오늘 임계 점수를 넘는 추가 후보 부족"

    def blank_slot_html(slot_num: int, reason: str) -> str:
        return (
            f'<div style="border:1px dashed #CBD5E1;background:#F8FAFC;border-radius:12px;'
            f'padding:14px;margin-bottom:10px;text-align:center">'
            f'<div style="font-size:22px;opacity:.45">🔍</div>'
            f'<div style="font-weight:700;font-size:12px;color:#475569;margin:6px 0 4px">#{slot_num} 추천 종목 없음</div>'
            f'<div style="font-size:10px;color:#6B7280;line-height:1.5">{reason}</div>'
            f'<div style="font-size:9px;color:#94A3B8;margin-top:6px">💡 강제로 채우지 않습니다</div>'
            f'</div>'
        )

    rank_colors = ["#F59E0B", "#94A3B8", "#A16207", "#0B1B3D", "#0B1B3D"]
    TARGET_TECH = 4
    for market, label in [("kr", "🇰🇷 코스피·코스닥"), ("us", "🇺🇸 나스닥·NYSE"), ("futures", "📊 선물·ETF")]:
        market_block = data.get(market) or {}
        picks = market_block.get("picks") or []
        quant_pick = market_block.get("quant_pick")
        if not picks and not quant_pick: continue
        html.append(f'<h3 style="margin:18px 0 6px 0;font-size:14px;border-bottom:2px solid #1E3A8A;padding-bottom:5px">{label}</h3>')
        # 시장별 퀀트 픽 (재무재표 기반) 먼저
        if quant_pick:
            html.append(_market_quant_pick_html(quant_pick, market))
        # 기술 분석 TOP N + 부족분 블랭크 (시장 KR/US만 4개 기준, 선물은 그대로)
        if picks:
            tech_target = TARGET_TECH if market in ("kr", "us") else len(picks)
            html.append(f'<div style="font-size:10px;font-weight:600;color:#6B7280;margin:10px 0 6px 0;letter-spacing:.3px">📈 기술적 분석 TOP {tech_target}</div>')
        for idx, p in enumerate(picks, 1):
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

            # 모바일 우선: 헤더 좌측 순위 배지 → 우측 가격 + verdict 점수 블록
            score_html = _score_block_html(score, sector)
            rank_color = rank_colors[idx-1] if idx <= 5 else "#0B1B3D"
            rank_badge = (f'<span style="display:inline-block;width:22px;height:22px;border-radius:50%;'
                          f'background:{rank_color};color:#fff;font-weight:800;font-size:11px;'
                          f'text-align:center;line-height:22px;margin-right:6px;vertical-align:middle">'
                          f'#{idx}</span>')
            html.append(f'''
<div style="border:1px solid #EAEAF0;border-radius:12px;padding:12px;margin-bottom:10px">
  <div style="font-size:14px;font-weight:700;line-height:1.3">
    {rank_badge}{ticker} <span style="color:#6B7280;font-weight:500;font-size:12px">{cname}</span>
  </div>
  <div style="margin-top:4px;font-size:16px;font-weight:800;color:#0B1B3D">{price_fmt}</div>
  {score_html}
  <div style="margin-top:8px;font-size:12px;color:#374151;line-height:1.5">{one_liner}</div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-top:8px;font-size:12px;border-collapse:separate;border-spacing:0 3px">
    <tr>
      <td style="background:#F1F5F9;padding:8px 10px;border-radius:6px;font-weight:600;color:#334155;white-space:nowrap;width:65px">📥 매수가</td>
      <td style="background:#F1F5F9;padding:8px 10px;border-radius:6px;font-weight:700;text-align:right;white-space:nowrap;font-size:12px">{elo} ~ {ehi}</td>
    </tr>
    <tr>
      <td style="background:#FEE2E2;padding:8px 10px;border-radius:6px;font-weight:600;color:#991B1B;white-space:nowrap;width:65px">🛑 손절가</td>
      <td style="background:#FEE2E2;padding:8px 10px;border-radius:6px;font-weight:700;color:#991B1B;text-align:right;white-space:nowrap">{stp}</td>
    </tr>
    <tr>
      <td style="background:#DCFCE7;padding:8px 10px;border-radius:6px;font-weight:600;color:#166534;white-space:nowrap;width:65px">🎯 목표가</td>
      <td style="background:#DCFCE7;padding:8px 10px;border-radius:6px;font-weight:700;color:#166534;text-align:right;white-space:nowrap">{tgt}</td>
    </tr>
  </table>
</div>''')

        # 부족분 블랭크 슬롯 추가 (KR/US 만 4개 기준)
        if market in ("kr", "us") and len(picks) < TARGET_TECH:
            reason = blank_reason(market)
            for slot in range(len(picks) + 1, TARGET_TECH + 1):
                html.append(blank_slot_html(slot, reason))

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
