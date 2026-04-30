"""
스크리너 결과 → 랜딩 페이지가 읽는 JSON 으로 발행.

GitHub Actions 자동 호출:
  - KR 갱신: 평일 KST 08:00, 08:25 (한국장 09:00 시작 직전)
  - US 갱신: 평일 KST 21:00, 21:55 (미장 22:30~23:30 시작 직전)
  - 선물·ETF: 한국장 + 미국장 갱신 시점에 함께 갱신

JSON 스키마:
{
  "updated_at_iso": ..., "updated_at_kst": ...,
  "fear": {
    "vkospi": { "value": 18.4, "label": "안정", "light": "🟢", "summary": "..." },
    "vix":    { "value": 16.4, "label": "안정", "light": "🟢", "summary": "..." }
  },
  "kr":      { picks: [...], traffic_light, updated_at_* },
  "us":      { picks: [...], traffic_light, updated_at_* },
  "futures": { picks: [...], updated_at_* }
}
"""
from __future__ import annotations
import os, json, logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import KST, TOP_N_KR, TOP_N_US, QUALITY, KR_SECTOR_BONUS
from .screener_us import screen_us, market_traffic_light
from .screener_kr import screen_kr
from .screener_futures import screen_futures
from . import data_sources as ds
from . import narrative

log = logging.getLogger(__name__)
PICKS_PATH = Path(os.getenv("PICKS_JSON", "landing/data/picks.json"))
RUNTIME_PATH = Path(os.getenv("RUNTIME_CONFIG_JSON", "landing/data/runtime_config.json"))


def _load_runtime() -> dict:
    """런타임 설정 — market_mode, sector_blacklist, 임계값 override 등.
    파일 없거나 파싱 실패 시 기본값 (normal mode) 반환."""
    if not RUNTIME_PATH.exists():
        return {"market_mode": "normal", "geopolitical_note": "",
                "sector_blacklist": [], "sector_bonus_overrides": {},
                "min_score_kr_override": None, "min_score_us_override": None,
                "fear_index_max_override": None}
    try:
        return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("runtime_config 파싱 실패 → normal mode: %s", e)
        return {"market_mode": "normal"}


def _apply_runtime_overrides(picks_kr: list, picks_us: list, rt: dict) -> tuple[list, list]:
    """런타임 설정으로 picks 후처리 — 섹터 블랙리스트 / 보너스 덮어쓰기 / 점수 재계산."""
    blacklist = set(rt.get("sector_blacklist") or [])
    bonus_overrides: dict = rt.get("sector_bonus_overrides") or {}

    def _filter_and_rescore(picks: list, market: str) -> list:
        out = []
        for p in picks:
            sec = getattr(p, "sector", "")
            if sec in blacklist:
                continue
            # 섹터 보너스 덮어쓰기 — 기본 보너스를 빼고 override 보너스를 더함
            if market == "kr" and bonus_overrides:
                base_bonus = KR_SECTOR_BONUS.get(sec, 0)
                if sec in bonus_overrides:
                    p.score = max(0, p.score - base_bonus + float(bonus_overrides[sec]))
            out.append(p)
        out.sort(key=lambda x: x.score, reverse=True)
        return out

    return _filter_and_rescore(picks_kr, "kr"), _filter_and_rescore(picks_us, "us")


def _now_kst() -> datetime:
    return datetime.now(KST)


def _format_kst(dt: datetime) -> str:
    weekday = "월화수목금토일"[dt.weekday()]
    return f"{dt.strftime('%Y.%m.%d')} ({weekday}) {dt.strftime('%H:%M')} KST"


def _load_existing() -> dict:
    if PICKS_PATH.exists():
        try:
            return json.loads(PICKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


class _NumpyJSON(json.JSONEncoder):
    """numpy bool_/int64/float64 등을 Python 기본 타입으로 변환."""
    def default(self, o):
        try:
            import numpy as np
            if isinstance(o, np.bool_): return bool(o)
            if isinstance(o, (np.integer,)): return int(o)
            if isinstance(o, (np.floating,)): return float(o)
            if isinstance(o, np.ndarray): return o.tolist()
        except ImportError: pass
        if hasattr(o, "isoformat"): return o.isoformat()
        return str(o)


def _write(data: dict) -> None:
    PICKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PICKS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, cls=_NumpyJSON),
        encoding="utf-8")
    log.info("picks.json written: %s", PICKS_PATH)


# ---- 공포지수 분류 -------------------------------------------------------

def _vix_meta(v: float) -> dict:
    if v <= 18: light, label, msg = "🟢", "안정", "공포지수 낮아 변동성 안정"
    elif v <= 25: light, label, msg = "🟡", "주의", "변동성 다소 높음 — 평소의 70% 사이즈로"
    else: light, label, msg = "🔴", "위험", "변동성 매우 높음 — 신규 진입 자제"
    return {"value": round(v, 2), "label": label, "light": light, "summary": msg}


def _vkospi_meta(v: float) -> dict:
    if v <= 18: light, label, msg = "🟢", "안정", "한국장 변동성 안정"
    elif v <= 25: light, label, msg = "🟡", "주의", "한국장 변동성 상승 — 분할매수 권장"
    else: light, label, msg = "🔴", "위험", "한국장 변동성 매우 높음 — 신규 진입 자제"
    return {"value": round(v, 2), "label": label, "light": light, "summary": msg}


# ---- 후보 → JSON dict + narrative 부착 ----------------------------------

def _to_json_kr(c) -> dict:
    d = asdict(c); d["narrative"] = narrative.narrate_kr(c); return d

def _to_json_us(c) -> dict:
    d = asdict(c); d["narrative"] = narrative.narrate_us(c); return d

def _to_json_fut(c) -> dict:
    d = asdict(c); d["narrative"] = narrative.narrate_futures(c); return d


# ---- Publisher ----------------------------------------------------------

def _effective_threshold(default: float, override, mode: str, defensive_bump: float = 20) -> float:
    """런타임 임계값 — override > mode 부스트 > 기본값."""
    if override is not None:
        return float(override)
    if mode == "defensive":
        return float(default + defensive_bump)
    return float(default)


def _gate_kr(picks: list, vkospi: float, rt: dict) -> tuple[list, str | None]:
    """KR 품질 게이트 — 공포지수 + 점수 임계 + 런타임 mode 반영."""
    mode = (rt.get("market_mode") or "normal").lower()
    if mode == "crisis":
        return [], (f"⚠️ 위기 모드 활성화 — 신규 추천 전면 보류. "
                    f"{rt.get('geopolitical_note') or '시장 상황 모니터링 중'}")
    fear_max = float(rt.get("fear_index_max_override") or QUALITY.vkospi_max)
    if vkospi > fear_max:
        return [], (f"한국장 변동성 매우 높음 (VKOSPI {vkospi:.1f} > {fear_max:.0f}) — "
                    "신규 진입 자제 권장. 오늘은 미추천")
    threshold = _effective_threshold(QUALITY.min_score_kr,
                                     rt.get("min_score_kr_override"), mode)
    qualified = [p for p in picks if (p.score or 0) >= threshold]
    if not qualified:
        top = picks[0].score if picks else 0
        mode_note = " (방어 모드)" if mode == "defensive" else ""
        return [], (f"오늘 코스피·코스닥에서 추천 임계({threshold:.0f}점{mode_note})를 넘는 종목 없음 "
                    f"(최고 {top:.0f}점). 약한 신호로 무리하게 진입하지 않습니다")
    return qualified, None


def _gate_us(picks: list, vix: float, rt: dict) -> tuple[list, str | None]:
    """US 품질 게이트 — VIX + 점수 임계 + 런타임 mode 반영."""
    mode = (rt.get("market_mode") or "normal").lower()
    if mode == "crisis":
        return [], (f"⚠️ 위기 모드 활성화 — 신규 추천 전면 보류. "
                    f"{rt.get('geopolitical_note') or '시장 상황 모니터링 중'}")
    fear_max = float(rt.get("fear_index_max_override") or QUALITY.vix_max)
    if vix > fear_max:
        return [], (f"미장 변동성 매우 높음 (VIX {vix:.1f} > {fear_max:.0f}) — "
                    "신규 진입 자제 권장. 오늘은 미추천")
    threshold = _effective_threshold(QUALITY.min_score_us,
                                     rt.get("min_score_us_override"), mode)
    qualified = [p for p in picks if (p.score or 0) >= threshold]
    if not qualified:
        top = picks[0].score if picks else 0
        mode_note = " (방어 모드)" if mode == "defensive" else ""
        return [], (f"오늘 미장에서 추천 임계({threshold:.0f}점{mode_note})를 넘는 종목 없음 "
                    f"(최고 {top:.0f}점). 약한 신호로 무리하게 진입하지 않습니다")
    return qualified, None


def publish_kr() -> dict:
    log.info("publish KR start")
    rt = _load_runtime()
    picks = screen_kr(top_n=TOP_N_KR * 3)  # 3배수로 뽑아 블랙리스트·재정렬 후 자르기
    picks, _ = _apply_runtime_overrides(picks, [], rt)
    picks = picks[:TOP_N_KR]
    now = _now_kst()
    payload = _load_existing()
    payload.setdefault("fear", {})
    vkospi = ds.vkospi_close()
    payload["fear"]["vkospi"] = _vkospi_meta(vkospi)
    payload["runtime"] = {
        "market_mode": rt.get("market_mode", "normal"),
        "geopolitical_note": rt.get("geopolitical_note", ""),
        "updated_at_kst": rt.get("updated_at_kst", ""),
    }

    qualified, no_reason = _gate_kr(picks, vkospi, rt)
    if no_reason:
        log.info("KR 미추천: %s", no_reason)
        payload["kr"] = {
            "picks": [],
            "no_pick_reason": no_reason,
            "updated_at_iso": now.isoformat(),
            "updated_at_kst": _format_kst(now),
        }
    else:
        payload["kr"] = {
            "picks": [_to_json_kr(p) for p in qualified],
            "updated_at_iso": now.isoformat(),
            "updated_at_kst": _format_kst(now),
        }
    payload["updated_at_iso"] = now.isoformat()
    payload["updated_at_kst"] = _format_kst(now)
    _write(payload)
    return {"market": "kr", "n": len(qualified),
            "tickers": [p.ticker for p in qualified],
            "no_pick_reason": no_reason}


def publish_us() -> dict:
    log.info("publish US start")
    rt = _load_runtime()
    picks = screen_us(top_n=TOP_N_US * 3)
    _, picks = _apply_runtime_overrides([], picks, rt)
    picks = picks[:TOP_N_US]
    light = market_traffic_light()
    now = _now_kst()
    payload = _load_existing()
    payload.setdefault("fear", {})
    payload["fear"]["vix"] = _vix_meta(light["vix"])
    payload["runtime"] = {
        "market_mode": rt.get("market_mode", "normal"),
        "geopolitical_note": rt.get("geopolitical_note", ""),
        "updated_at_kst": rt.get("updated_at_kst", ""),
    }

    qualified, no_reason = _gate_us(picks, light["vix"], rt)
    if no_reason:
        log.info("US 미추천: %s", no_reason)
        payload["us"] = {
            "picks": [],
            "no_pick_reason": no_reason,
            "traffic_light": light,
            "updated_at_iso": now.isoformat(),
            "updated_at_kst": _format_kst(now),
        }
    else:
        payload["us"] = {
            "picks": [_to_json_us(p) for p in qualified],
            "traffic_light": light,
            "updated_at_iso": now.isoformat(),
            "updated_at_kst": _format_kst(now),
        }
    payload["updated_at_iso"] = now.isoformat()
    payload["updated_at_kst"] = _format_kst(now)
    _write(payload)
    return {"market": "us", "n": len(qualified),
            "tickers": [p.ticker for p in qualified],
            "vix": light["vix"], "no_pick_reason": no_reason}


def publish_futures() -> dict:
    log.info("publish FUTURES start")
    picks = screen_futures()
    now = _now_kst()
    payload = _load_existing()

    if not picks and payload.get("futures", {}).get("picks"):
        log.warning("Futures picks 비어있음 — 직전 결과 보존")
        return {"market": "futures", "n": 0, "preserved_previous": True}

    payload["futures"] = {
        "picks": [_to_json_fut(p) for p in picks],
        "updated_at_iso": now.isoformat(),
        "updated_at_kst": _format_kst(now),
    }
    payload["updated_at_iso"] = now.isoformat()
    payload["updated_at_kst"] = _format_kst(now)
    _write(payload)
    return {"market": "futures", "n": len(picks), "tickers": [p.ticker for p in picks]}


def publish_both() -> dict:
    return {"kr": publish_kr(), "us": publish_us(), "futures": publish_futures()}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "both"
    if cmd == "kr": print(publish_kr())
    elif cmd == "us": print(publish_us())
    elif cmd == "futures": print(publish_futures())
    else: print(publish_both())
