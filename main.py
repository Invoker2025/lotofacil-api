from __future__ import annotations
import logging
import os
import re
import json
import time
import datetime as dt
import asyncio  # Para a pausa na coleta
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
print(">>> MAIN.PY NOVO COM /SIMULATE CARREGADO <<<")

# Lotofacil API Ã¢â‚¬â€œ v6.5.1
# - Coleta resultados da LotofÃƒÂ¡cil com 3 nÃƒÂ­veis:
#     1) Mirror pÃƒÂºblico (opcionalmente preferido)
#     2) JSON oficial (Portal de Loterias CAIXA)
#     3) HTML oficial (pÃƒÂ¡gina de resultados: scraping tolerante)
# - UI simples em /app; /ready mostra latest_contest; ÃƒÂ­cones e PWA em /static.


# ----------------------------------------------------------------------
# Paths / versÃƒÂ£o
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
APP_VERSION = "6.5.1"

# ----------------------------------------------------------------------
# ConfiguraÃƒÂ§ÃƒÂ£o de Logging
# ----------------------------------------------------------------------
# Configura logging detalhado
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Cria logger principal
logger = logging.getLogger("lotofacil_api")
logger.setLevel(logging.INFO)

# Handler para console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Desativa logs do uvicorn se quiser menos ruÃƒÂ­do
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

logger.info(f"Ã°Å¸Å½Â¯ Lotofacil API v{APP_VERSION} iniciando...")
# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
app = FastAPI(title="LotofÃƒÂ¡cil API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
# Sirva a pasta "static" (ÃƒÂ­cones/manifest/sw)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- DIAGNÃƒâ€œSTICO /static (ÃƒÂºtil pra 404) -------------------------------
logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
async def _log_static_at_startup():
    try:
        files = sorted(os.listdir(STATIC_DIR))
        logger.info(f"[STATIC] dir = {STATIC_DIR.resolve()}")
        logger.info(f"[STATIC] files = {files}")
    except Exception as e:
        logger.error(f"[STATIC] erro listando: {e}")


@app.get("/_debug/static")
def _debug_static():
    try:
        return {
            "cwd": os.getcwd(),
            "static_dir": str(STATIC_DIR.resolve()),
            "files": sorted(os.listdir(STATIC_DIR)),
        }
    except Exception as e:
        return {"error": str(e)}
# ----------------------------------------------------------------------


# --- Origens CAIXA (JSON oficial) ---
CAIXA_HOSTS = [
    "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil",
    "https://loterias.caixa.gov.br/portaldeloterias/api/lotofacil",
]

# --- PÃƒÂ¡gina oficial (HTML) para scraping ---
CAIXA_HTML_URLS = [
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx",
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx?concurso={n}",
]

# --- Mirror pÃƒÂºblico (somente leitura) ---
MIRROR_LATEST = "https://loteriascaixa-api.herokuapp.com/api/lotofacil/latest"
MIRROR_BY_ID = "https://loteriascaixa-api.herokuapp.com/api/lotofacil/{n}"

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
AGG_TTL_SEC = int(os.getenv("AGG_TTL_SEC", "120"))
CAIXA_TTL_SEC = int(os.getenv("CAIXA_TTL_SEC", "120"))
PREFER_MIRROR = os.getenv("PREFER_MIRROR", "0") == "1"

# ----------------------------------------------------------------------
# HTTP client + caches
# ----------------------------------------------------------------------
_http: httpx.AsyncClient | None = None
_caixa_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_agg_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# >>>>>>>>>>>> NOVO: timezone BRT (UTC-3) para carimbar updated_at <<<<<<<<<<<<
BRT = dt.timezone(dt.timedelta(hours=-3), name="BRT")


async def ensure_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0 Safari/537.36"),
                "accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "pragma": "no-cache",
                "cache-control": "no-cache",
                "referer": "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx",
                "origin": "https://loterias.caixa.gov.br",
            },
            follow_redirects=True,
        )
    return _http


async def close_http():
    global _http
    try:
        if _http is not None:
            await _http.aclose()
    finally:
        _http = None


def _cache_get(store: Dict[str, Tuple[float, Any]], key: str, ttl: int) -> Any | None:
    ent = store.get(key)
    if not ent:
        return None
    ts, payload = ent
    if time.time() - ts <= ttl:
        return payload
    return None


def _cache_put(store: Dict[str, Tuple[float, Any]], key: str, payload: Any):
    store[key] = (time.time(), payload)


def _with_ts(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out["_ts"] = time.time()
    return out


def _agg_key(kind: str, **params) -> str:
    return f"{kind}:{json.dumps(params, sort_keys=True)}"


def _agg_get(kind: str, **params) -> Dict[str, Any] | None:
    return _cache_get(_agg_cache, _agg_key(kind, **params), AGG_TTL_SEC)


def _agg_put(payload: Dict[str, Any], kind: str, **params):
    _cache_put(_agg_cache, _agg_key(kind, **params), _with_ts(payload))

# ----------------------------------------------------------------------
# Utils
# ----------------------------------------------------------------------


def validate_parity(even: int, odd: int) -> Tuple[int, int]:
    if even + odd != 15:
        raise HTTPException(
            status_code=422,
            detail="Paridade invÃƒÂ¡lida: even + odd deve ser igual a 15"
        )
    return even, odd


def parse_draw_date(s: str) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        d, M, y = m.groups()
        d, M, y = int(d), int(M), int(y)
        if y < 100:
            y += 2000
        try:
            return dt.date(y, M, d)
        except Exception:
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, M, d = map(int, m.groups())
        try:
            return dt.date(y, M, d)
        except Exception:
            return None
    return None


def valid_15_unique(nums: List[int]) -> bool:
    return len(nums) == 15 and len(set(nums)) == 15 and all(1 <= n <= 25 for n in nums)


def histogram_even_odd(numbers: List[int]) -> Tuple[int, int]:
    e = sum(1 for n in numbers if n % 2 == 0)
    return e, 15 - e


def summarize_draws(draws: List[dict]) -> Dict[str, Any]:
    hist = {"7-8": 0, "8-7": 0, "outros": 0}
    total = max(1, len(draws))
    evens, odds = [], []
    for d in draws:
        e, o = histogram_even_odd(d["numbers"])
        if e == 7 and o == 8:
            hist["7-8"] += 1
        elif e == 8 and o == 7:
            hist["8-7"] += 1
        else:
            hist["outros"] += 1
        evens.append(e)
        odds.append(o)
    return {
        "histogram": hist,
        "avg_even": round(sum(evens) / total, 1),
        "avg_odd":  round(sum(odds) / total, 1),
    }


def frequencies(draws: List[dict]) -> List[Dict[str, Any]]:
    counts = {n: 0 for n in range(1, 26)}
    total = max(1, len(draws))
    for d in draws:
        for x in d.get("numbers", []):
            counts[x] += 1
    return [{"n": n, "count": counts[n], "pct": round((counts[n]/total)*100.0, 1)} for n in range(1, 26)]


def classify_trend(draws: List[dict], window: int = 20):
    """Classifica dezenas em quentes, mornas e frias"""
    if not draws or len(draws) < window:
        window = len(draws)

    draws_sorted = sorted(
        draws,
        key=lambda d: int(d["contest"]),
        reverse=True
    )
    recent = draws_sorted[:window]

    counts = {n: 0 for n in range(1, 26)}
    for d in recent:
        for n in d["numbers"]:
            counts[n] += 1

    # AJUSTE OS LIMITES PARA SEREM MAIS RESTRITIVOS:
    # Quentes: apareceram em 70%+ dos ÃƒÂºltimos concursos
    # Mornas: apareceram em 30%-70%
    # Frias: apareceram em menos de 30%

    hot = [n for n, c in counts.items() if c >= int(window * 0.7)]  # 70%+
    warm = [n for n, c in counts.items() if int(window * 0.3) <= c <
            int(window * 0.7)]  # 30%-70%
    cold = [n for n, c in counts.items() if c < int(window * 0.3)
            ]  # menos de 30%

    return {
        "hot": sorted(hot),
        "warm": sorted(warm),
        "cold": sorted(cold),
        "counts": counts,
        "window_used": window
    }


def number_band(n: int) -> str:
    if n <= 9:
        return "low"
    if n <= 19:
        return "mid"
    return "high"


def number_gap(draws: List[dict], n: int) -> int:
    for idx, d in enumerate(draws):
        if n in set(d.get("numbers", [])):
            return idx
    return len(draws) + 1


def percentage_system_numbers(draws: List[dict]) -> Dict[int, Dict[str, Any]]:
    """
    Regra pratica inspirada no sistema de porcentagem do Livro Negro.

    No livro, a regra original usa pelo menos 2 aparicoes nos ultimos 10
    concursos e 1 nos ultimos 5. Na Lotofacil saem 15 de 25 dezenas por
    concurso, entao esse corte fica permissivo demais. Aqui usamos um corte
    adaptado: pelo menos 6 aparicoes nos ultimos 10 e 3 nos ultimos 5.
    """
    last_10 = draws[:min(10, len(draws))]
    last_5 = draws[:min(5, len(draws))]

    out: Dict[int, Dict[str, Any]] = {}
    for n in range(1, 26):
        count_10 = sum(1 for d in last_10 if n in set(d.get("numbers", [])))
        count_5 = sum(1 for d in last_5 if n in set(d.get("numbers", [])))
        out[n] = {
            "last_10": count_10,
            "last_5": count_5,
            "qualified": count_10 >= 6 and count_5 >= 3,
        }
    return out


def band_targets(needed: int, parity: str) -> Dict[str, int]:
    if needed <= 0:
        return {"low": 0, "mid": 0, "high": 0}
    if needed == 8:
        return {"low": 3, "mid": 3, "high": 2}
    if needed == 7:
        return {"low": 2, "mid": 3, "high": 2} if parity == "even" else {"low": 3, "mid": 2, "high": 2}
    if needed == 6:
        return {"low": 2, "mid": 2, "high": 2}
    if needed == 9:
        return {"low": 3, "mid": 4, "high": 2}

    targets = {"low": needed // 3, "mid": needed // 3, "high": needed // 3}
    for band in ("mid", "low", "high"):
        if sum(targets.values()) < needed:
            targets[band] += 1
    return targets


def build_number_scores(
    draws: List[dict],
    hot: List[int],
    warm: List[int],
    cold: List[int],
) -> List[Dict[str, Any]]:
    total = max(1, len(draws))
    recent = draws[:min(20, len(draws))]
    recent_total = max(1, len(recent))
    last_draw = set(draws[0].get("numbers", [])) if draws else set()

    long_counts = {f["n"]: f["count"] for f in frequencies(draws)}
    recent_counts = {f["n"]: f["count"] for f in frequencies(recent)}
    percentage_profile = percentage_system_numbers(draws)

    out = []
    for n in range(1, 26):
        gap = number_gap(draws, n)
        pct_info = percentage_profile[n]
        long_score = (long_counts.get(n, 0) / total) * 42
        recent_score = (recent_counts.get(n, 0) / recent_total) * 34
        repeat_score = 8 if n in last_draw else 0
        # Mantido como indicador auditavel. No backtest, usar isso como boost
        # direto piorou a media recente da Lotofacil, entao nao altera o score.
        percentage_score = 0
        if 1 <= gap <= 4:
            gap_score = 8
        elif gap == 0:
            gap_score = 3
        elif 5 <= gap <= 9:
            gap_score = 4
        elif gap >= 14:
            gap_score = -4
        else:
            gap_score = 0

        trend_score = 4 if n in hot else 6 if n in warm else 1 if n in cold else 0
        score = long_score + recent_score + repeat_score + gap_score + trend_score + percentage_score
        out.append({
            "n": n,
            "count": long_counts.get(n, 0),
            "recent_count": recent_counts.get(n, 0),
            "last_10_count": pct_info["last_10"],
            "last_5_count": pct_info["last_5"],
            "percentage_qualified": pct_info["qualified"],
            "gap": gap,
            "band": number_band(n),
            "score": round(score, 4),
        })
    return out


def select_balanced_numbers(
    scored: List[Dict[str, Any]],
    needed: int,
    parity: str,
) -> List[Dict[str, Any]]:
    candidates = [x for x in scored if (x["n"] % 2 == 0) == (parity == "even")]
    candidates = sorted(candidates, key=lambda x: (-x["score"], -x["recent_count"], -x["count"], x["n"]))
    targets = band_targets(needed, parity)

    selected: List[Dict[str, Any]] = []
    selected_nums = set()

    for band in ("low", "mid", "high"):
        band_items = [x for x in candidates if x["band"] == band and x["n"] not in selected_nums]
        for item in band_items[:targets.get(band, 0)]:
            selected.append(item)
            selected_nums.add(item["n"])

    if len(selected) < needed:
        for item in candidates:
            if item["n"] not in selected_nums:
                selected.append(item)
                selected_nums.add(item["n"])
            if len(selected) >= needed:
                break

    return selected[:needed]


def intelligent_exclusions(
    scored: List[Dict[str, Any]],
    combo: List[int],
    count: int = 3
) -> Dict[str, Any]:
    combo_set = set(combo)
    candidates = [x for x in scored if x["n"] not in combo_set]

    ranked = sorted(
        candidates,
        key=lambda x: (
            x["score"],
            x["recent_count"],
            x["count"],
            -x["gap"],
            x["n"],
        )
    )
    picked = ranked[:count]

    return {
        "exclude_2": [x["n"] for x in picked[:2]],
        "exclude_3": [x["n"] for x in picked[:3]],
        "candidates": [
            {
                "n": x["n"],
                "score": x["score"],
                "recent_count": x["recent_count"],
                "last_10_count": x.get("last_10_count"),
                "last_5_count": x.get("last_5_count"),
                "gap": x["gap"],
            }
            for x in picked
        ],
        "rule": "menor score fora da combinacao principal",
    }


def build_derived_games(
    scored: List[Dict[str, Any]],
    combo: List[int],
    even_needed: int,
    odd_needed: int,
    exclusions: Optional[List[int]] = None,
    total_games: int = 6,
) -> List[Dict[str, Any]]:
    score_by_n = {x["n"]: x for x in scored}
    primary = sorted(combo)
    exclude_set = set(exclusions or [])

    games: List[Dict[str, Any]] = [{
        "index": 1,
        "role": "principal",
        "numbers": primary,
        "pattern": f"{even_needed}-{odd_needed}",
        "sum": sum(primary),
        "valid": valid_15_unique(primary) and valid_sum(primary),
        "changed_out": [],
        "changed_in": [],
    }]

    selected = set(primary)
    selected_even = sorted(
        [n for n in primary if n % 2 == 0],
        key=lambda n: (score_by_n.get(n, {}).get("score", 0), n)
    )
    selected_odd = sorted(
        [n for n in primary if n % 2 == 1],
        key=lambda n: (score_by_n.get(n, {}).get("score", 0), n)
    )

    alt_even = sorted(
        [x["n"] for x in scored if x["n"] % 2 == 0 and x["n"] not in selected and x["n"] not in exclude_set],
        key=lambda n: (-score_by_n[n]["score"], -score_by_n[n]["recent_count"], n)
    )
    alt_odd = sorted(
        [x["n"] for x in scored if x["n"] % 2 == 1 and x["n"] not in selected and x["n"] not in exclude_set],
        key=lambda n: (-score_by_n[n]["score"], -score_by_n[n]["recent_count"], n)
    )

    swap_templates = [
        (1, 0),
        (0, 1),
        (1, 1),
        (2, 1),
        (1, 2),
    ]

    seen = {tuple(primary)}
    for even_swaps, odd_swaps in swap_templates:
        if len(games) >= total_games:
            break
        if len(selected_even) < even_swaps or len(alt_even) < even_swaps:
            continue
        if len(selected_odd) < odd_swaps or len(alt_odd) < odd_swaps:
            continue

        out_even = selected_even[:even_swaps]
        out_odd = selected_odd[:odd_swaps]
        in_even = alt_even[:even_swaps]
        in_odd = alt_odd[:odd_swaps]

        candidate = sorted((selected - set(out_even) - set(out_odd)) | set(in_even) | set(in_odd))
        key = tuple(candidate)
        if key in seen or not valid_15_unique(candidate):
            continue

        seen.add(key)
        games.append({
            "index": len(games) + 1,
            "role": "derivado",
            "numbers": candidate,
            "pattern": f"{even_needed}-{odd_needed}",
            "sum": sum(candidate),
            "valid": valid_sum(candidate),
            "changed_out": sorted(out_even + out_odd),
            "changed_in": sorted(in_even + in_odd),
        })

    return games


def consecutive_profile(numbers: List[int]) -> Dict[str, Any]:
    nums = sorted(numbers)
    groups: List[List[int]] = []
    current: List[int] = []

    for n in nums:
        if not current or n == current[-1] + 1:
            current.append(n)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [n]

    if len(current) >= 2:
        groups.append(current)

    max_run = max((len(g) for g in groups), default=1)
    return {
        "groups": groups,
        "groups_count": len(groups),
        "max_run": max_run,
        "has_long_run": max_run >= 5,
        "status": "attention" if max_run >= 5 else "ok",
    }


def explain_ranked_numbers(scored: List[Dict[str, Any]], combo: List[int]) -> Dict[str, Any]:
    selected = set(combo)
    ranked = sorted(scored, key=lambda x: (-x["score"], -x["recent_count"], -x["count"], x["n"]))

    def item(x: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "n": x["n"],
            "score": x["score"],
            "status": "selecionada" if x["n"] in selected else "fora",
            "parity": "par" if x["n"] % 2 == 0 else "impar",
            "band": x["band"],
            "long_count": x["count"],
            "recent_count": x["recent_count"],
            "last_10_count": x.get("last_10_count"),
            "last_5_count": x.get("last_5_count"),
            "gap": x["gap"],
            "percentage_qualified": x.get("percentage_qualified", False),
        }

    return {
        "strong": [item(x) for x in ranked[:8]],
        "selected": [item(x) for x in ranked if x["n"] in selected],
        "risk": [item(x) for x in sorted(scored, key=lambda x: (x["score"], x["recent_count"], x["count"], x["n"]))[:8]],
    }


def apply_weight_profile(scored: List[Dict[str, Any]], profile: Dict[str, float]) -> List[Dict[str, Any]]:
    out = []
    for x in scored:
        gap = x["gap"]
        if 1 <= gap <= 4:
            gap_signal = 1.0
        elif 5 <= gap <= 9:
            gap_signal = 0.5
        elif gap >= 14:
            gap_signal = -0.5
        else:
            gap_signal = 0.0

        weighted_score = (
            x["count"] * profile.get("long", 1.0)
            + x["recent_count"] * profile.get("recent", 1.0)
            + (1 if gap <= 4 else 0) * profile.get("recency", 1.0)
            + gap_signal * profile.get("gap", 1.0)
            + (1 if x.get("percentage_qualified") else 0) * profile.get("percentage", 0.0)
        )
        y = x.copy()
        y["score"] = round(weighted_score, 4)
        out.append(y)
    return out


def build_weighted_suggestion(
    draws: List[dict],
    even_needed: int,
    odd_needed: int,
    profile: Dict[str, float]
) -> Dict[str, Any]:
    trend = classify_trend(draws, window=20)
    base = build_number_scores(draws, trend.get("hot", []), trend.get("warm", []), trend.get("cold", []))
    scored = apply_weight_profile(base, profile)
    ev = select_balanced_numbers(scored, even_needed, "even")
    od = select_balanced_numbers(scored, odd_needed, "odd")
    combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])
    return {
        "combo": combo,
        "pattern": f"{even_needed}-{odd_needed}",
        "valid": valid_15_unique(combo) and valid_sum(combo),
        "meta": {"weight_profile": profile},
    }


def build_parity_suggestion_legacy(
    draws: List[dict],
    even_needed: int = 8,
    odd_needed: int = 7
) -> Dict[str, Any]:
    try:
        if not draws or len(draws) < 2:
            return {
                "even": [],
                "odd": [],
                "combo": [],
                "parity": {"even_count": even_needed, "odd_count": odd_needed},
                "pattern": f"{even_needed}-{odd_needed}",
                "valid": False,
                "rules": {"sum_ok": False, "repeat_ok": False},
                "error": "Draws insuficientes para analise",
            }

        even_needed = max(0, min(15, even_needed))
        odd_needed = max(0, min(15 - even_needed, odd_needed))
        if even_needed + odd_needed != 15:
            even_needed, odd_needed = 8, 7

        last_draw = draws[0]["numbers"] if draws else []
        trend = classify_trend(draws, window=20)
        allowed = set(trend.get("hot", []) + trend.get("warm", []))

        freq_all = frequencies(draws)
        freq = [f for f in freq_all if f["n"] in allowed]
        if not freq or len(freq) < 15:
            freq = freq_all

        ev_pool = [f for f in freq if f["n"] % 2 == 0]
        od_pool = [f for f in freq if f["n"] % 2 == 1]

        if even_needed > odd_needed:
            ev = sorted(ev_pool, key=lambda x: (-x["count"], x["n"]))[:even_needed]
            od = sorted(od_pool, key=lambda x: (x["count"], x["n"]))[:odd_needed]
        elif odd_needed > even_needed:
            ev = sorted(ev_pool, key=lambda x: (x["count"], x["n"]))[:even_needed]
            od = sorted(od_pool, key=lambda x: (-x["count"], x["n"]))[:odd_needed]
        else:
            ev = sorted(ev_pool, key=lambda x: (-x["count"], x["n"]))[:even_needed]
            od = sorted(od_pool, key=lambda x: (-x["count"], x["n"]))[:odd_needed]

        if len(ev) < even_needed or len(od) < odd_needed:
            sorted_all = sorted(freq_all, key=lambda x: (-x["count"], x["n"]))
            ev = [f for f in sorted_all if f["n"] % 2 == 0][:even_needed]
            od = [f for f in sorted_all if f["n"] % 2 == 1][:odd_needed]

        combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])
        valid_sum_ok = valid_sum(combo)
        valid_repeat_ok = limit_repetition(combo, last_draw, max_repeat=9)

        return {
            "even": [x["n"] for x in ev],
            "odd": [x["n"] for x in od],
            "combo": combo,
            "parity": {"even_count": even_needed, "odd_count": odd_needed},
            "pattern": f"{even_needed}-{odd_needed}",
            "valid": valid_sum_ok and valid_repeat_ok,
            "rules": {"sum_ok": valid_sum_ok, "repeat_ok": valid_repeat_ok},
            "meta": {
                "strategy": "legacy_frequency",
                "draws_analyzed": len(draws),
            },
        }
    except Exception as e:
        return {
            "even": [],
            "odd": [],
            "combo": [],
            "parity": {"even_count": even_needed, "odd_count": odd_needed},
            "pattern": f"{even_needed}-{odd_needed}",
            "valid": False,
            "rules": {"sum_ok": False, "repeat_ok": False},
            "error": f"Erro interno: {str(e)}",
            "meta": {"strategy": "legacy_frequency", "error": True},
        }


def build_parity_suggestion(
    draws: List[dict],
    even_needed: int = 8,
    odd_needed: int = 7
) -> Dict[str, Any]:

    # ======================================================
    # INÃƒÂCIO DO BLOCO DE TRATAMENTO DE ERROS
    # ======================================================
    try:
        logger.debug(
            f"[LIVRO NEGRO] Iniciando sugestÃƒÂ£o com {len(draws)} concursos")

        # ------------------------------------------------------
        # ValidaÃƒÂ§ÃƒÂ£o bÃƒÂ¡sica de entrada
        # ------------------------------------------------------
        if not draws or len(draws) < 2:
            logger.warning(
                f"[LIVRO NEGRO] Draws insuficientes: {len(draws) if draws else 0}")
            return {
                "even": [],
                "odd": [],
                "combo": [],
                "parity": {"even_count": even_needed, "odd_count": odd_needed},
                "pattern": f"{even_needed}-{odd_needed}",
                "valid": False,
                "rules": {"sum_ok": False, "repeat_ok": False},
                "error": "Draws insuficientes para anÃƒÂ¡lise"
            }

        # ------------------------------------------------------
        # DEBUG: Log dos primeiros concursos
        # ------------------------------------------------------
        if logger.isEnabledFor(logging.DEBUG):
            sample = [f"{d.get('contest', '?')}" for d in draws[:3]]
            logger.debug(
                f"[LIVRO NEGRO] Primeiros concursos: {', '.join(sample)}")
            logger.debug(
                f"[LIVRO NEGRO] Config: {even_needed} pares, {odd_needed} ÃƒÂ­mpares")

        # ------------------------------------------------------
        # SeguranÃƒÂ§a bÃƒÂ¡sica de parÃƒÂ¢metros
        # ------------------------------------------------------
        even_needed = max(0, min(15, even_needed))
        odd_needed = max(0, min(15 - even_needed, odd_needed))
        if even_needed + odd_needed != 15:
            even_needed, odd_needed = 8, 7
            logger.info(
                f"[LIVRO NEGRO] Paridade ajustada para {even_needed}-{odd_needed}")

        # ------------------------------------------------------
        # ÃƒÅ¡ltimo concurso (regra das repetidas)
        # ------------------------------------------------------
        last_draw = draws[0]["numbers"] if draws else []
        logger.debug(f"[LIVRO NEGRO] ÃƒÅ¡ltimo concurso: {sorted(last_draw)}")

        # ------------------------------------------------------
        # TENDÃƒÅ NCIA Ã¢â‚¬â€ Livro Negro (janela fixa = 20)
        # ------------------------------------------------------
        trend = classify_trend(draws, window=20)
        hot = trend.get("hot", [])
        warm = trend.get("warm", [])
        cold = trend.get("cold", [])

        logger.debug(f"[LIVRO NEGRO] Quentes: {sorted(hot)}")
        logger.debug(f"[LIVRO NEGRO] Mornas : {sorted(warm)}")
        logger.debug(f"[LIVRO NEGRO] Frias  : {sorted(cold)}")

        # ------------------------------------------------------
        # NOVA ESTRATEGIA: pontuacao composta + cotas por faixa.
        # Mantem a paridade escolhida, mas melhora a selecao interna
        # com frequencia longa, recencia, repeticao, atraso e distribuicao.
        # ------------------------------------------------------
        scored = build_number_scores(draws, hot, warm, cold)
        ev = select_balanced_numbers(scored, even_needed, "even")
        od = select_balanced_numbers(scored, odd_needed, "odd")

        combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])

        valid_sum_ok = valid_sum(combo)
        valid_repeat_ok = limit_repetition(combo, last_draw, max_repeat=9)
        consecutive = consecutive_profile(combo)
        valid = valid_sum_ok and valid_repeat_ok

        band_profile = {
            "even": {
                "low": sum(1 for x in ev if x["band"] == "low"),
                "mid": sum(1 for x in ev if x["band"] == "mid"),
                "high": sum(1 for x in ev if x["band"] == "high"),
            },
            "odd": {
                "low": sum(1 for x in od if x["band"] == "low"),
                "mid": sum(1 for x in od if x["band"] == "mid"),
                "high": sum(1 for x in od if x["band"] == "high"),
            },
        }
        percentage_pool = sorted(x["n"] for x in scored if x.get("percentage_qualified"))
        exclusions = intelligent_exclusions(scored, combo, count=3)
        derived_games = build_derived_games(
            scored,
            combo,
            even_needed,
            odd_needed,
            exclusions=exclusions["exclude_3"],
            total_games=6,
        )
        for game in derived_games:
            game["consecutive"] = consecutive_profile(game.get("numbers", []))
        number_ranking = explain_ranked_numbers(scored, combo)

        return {
            "even": [x["n"] for x in ev],
            "odd":  [x["n"] for x in od],
            "combo": combo,
            "exclusions": exclusions,
            "games": derived_games,
            "parity": {
                "even_count": even_needed,
                "odd_count": odd_needed
            },
            "pattern": f"{even_needed}-{odd_needed}",
            "valid": valid,
            "rules": {
                "sum_ok": valid_sum_ok,
                "repeat_ok": valid_repeat_ok,
                "consecutive": consecutive
            },
            "meta": {
                "hot_count": len(hot),
                "warm_count": len(warm),
                "cold_count": len(cold),
                "draws_analyzed": len(draws),
                "strategy": "balanced_score_v2",
                "derived_games_count": len(derived_games),
                "number_ranking": number_ranking,
                "band_profile": band_profile,
                "percentage_system": {
                    "qualified_numbers": percentage_pool,
                    "selected_qualified": sorted(n for n in combo if n in set(percentage_pool)),
                    "rule": "last_10>=6 and last_5>=3"
                }
            }
        }


    except Exception as e:
        logger.error(f"[LIVRO NEGRO] ERRO CRÃƒÂTICO: {str(e)}", exc_info=True)
        return {
            "even": [],
            "odd": [],
            "combo": [],
            "parity": {"even_count": even_needed, "odd_count": odd_needed},
            "pattern": f"{even_needed}-{odd_needed}",
            "valid": False,
            "rules": {"sum_ok": False, "repeat_ok": False},
            "error": f"Erro interno: {str(e)}",
            "meta": {"error": True}
        }
    # ======================================================
    # FIM DO BLOCO DE TRATAMENTO DE ERROS
    # ======================================================


def window_to_range(window: str) -> Tuple[Optional[dt.date], Optional[dt.date]]:
    today = dt.date.today()
    if window == "1y":
        window = "12m"
    m = re.fullmatch(r"(\d{1,2})m", window)
    if m:
        months = max(1, min(12, int(m.group(1))))
        year = today.year
        month = today.month - months
        while month <= 0:
            month += 12
            year -= 1
        start = dt.date(year, month, min(today.day, 28))
        return start, today
    if window == "all":
        return None, today
    return today - dt.timedelta(days=93), today

# ----------------------------------------------------------------------
# Normalizadores e coletores
# ----------------------------------------------------------------------


def _normalize_from_any(j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Aceita tanto o JSON oficial da CAIXA quanto o do mirror."""
    try:
        numero = j.get("numero")
        dezenas = j.get("listaDezenas")
        data = j.get("dataApuracao") or j.get("data")
        if numero is None:
            numero = j.get("concurso")
            dezenas = dezenas or j.get("dezenas")
        if numero is None or dezenas is None:
            return None
        n_concurso = int(str(numero))
        nums = [int(str(x)) for x in dezenas]
        if not valid_15_unique(nums):
            return None
        e, o = histogram_even_odd(nums)
        return {
            "contest": n_concurso,
            "date": str(data or ""),
            "numbers": nums,
            "even_count": e,
            "odd_count": o,
            "source": "caixa",
        }
    except Exception:
        return None


def limit_repetition(candidate: List[int], last_draw: List[int], max_repeat: int = 9) -> bool:
    repeated = len(set(candidate) & set(last_draw))
    return repeated <= max_repeat


def valid_sum(numbers: List[int], min_sum: int = 190, max_sum: int = 210) -> bool:
    total = sum(numbers)
    return min_sum <= total <= max_sum


async def _mirror_get_latest() -> Optional[Dict[str, Any]]:
    try:
        c = await ensure_http()
        r = await c.get(MIRROR_LATEST)
        r.raise_for_status()
        j = r.json()
        data = _normalize_from_any(j)
        if data:
            data["source"] = "mirror"
            return data
    except Exception:
        pass
    return None


async def _mirror_get_concurso(n: int) -> Optional[Dict[str, Any]]:
    try:
        c = await ensure_http()
        r = await c.get(MIRROR_BY_ID.format(n=n))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        if int(str(j.get("numero") or j.get("concurso") or 0)) != n:
            return None
        data = _normalize_from_any(j)
        if data:
            data["source"] = "mirror"
            return data
    except Exception:
        pass
    return None


async def _json_get_latest() -> Optional[Dict[str, Any]]:
    c = await ensure_http()
    for base in CAIXA_HOSTS:
        try:
            r = await c.get(base, params={"_": int(time.time()*1000)})
            r.raise_for_status()
            j = r.json()
            data = _normalize_from_any(j)
            if data:
                return data
        except Exception:
            continue
    return None


async def _json_get_concurso(n: int) -> Optional[Dict[str, Any]]:
    c = await ensure_http()
    ts = int(time.time()*1000)
    variants = []
    for base in CAIXA_HOSTS:
        variants.append((base, {"concurso": n, "_": ts}))
        variants.append((f"{base}/{n}", {"_": ts}))
    for url, params in variants:
        try:
            r = await c.get(url, params=params)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            j = r.json()
            if int(str(j.get("numero") or 0)) != n:
                continue
            data = _normalize_from_any(j)
            if data:
                return data
        except Exception:
            continue
    return None

_HTML_RE_CONCURSO = re.compile(
    r"Concurso\s+(\d+)\s*\((\d{2}/\d{2}/\d{4})\)", re.I)
# captura 0..29; filtraremos 1..25
_HTML_RE_NUM = re.compile(r"\b([0-2]?\d)\b")


def _pick_15_numbers_near(html: str, anchor: int) -> Optional[List[int]]:
    segment = html[anchor: anchor + 4000]
    raw = [int(x) for x in _HTML_RE_NUM.findall(segment)]
    nums: List[int] = []
    for v in raw:
        if 1 <= v <= 25:
            nums.append(v)
            if len(nums) == 15:
                break
    return nums if valid_15_unique(nums) else None


async def _html_get_latest() -> Optional[Dict[str, Any]]:
    c = await ensure_http()
    for url in CAIXA_HTML_URLS[:1]:
        try:
            r = await c.get(url)
            r.raise_for_status()
            h = r.text
            m = _HTML_RE_CONCURSO.search(h)
            if not m:
                continue
            concurso = int(m.group(1))
            data = m.group(2)
            nums = _pick_15_numbers_near(h, m.start())
            if not nums:
                continue
            e, o = histogram_even_odd(nums)
            return {"contest": concurso, "date": data, "numbers": nums,
                    "even_count": e, "odd_count": o, "source": "html"}
        except Exception:
            continue
    return None


async def _html_get_concurso(n: int) -> Optional[Dict[str, Any]]:
    c = await ensure_http()
    for tpl in CAIXA_HTML_URLS:
        try:
            url = tpl.format(n=n) if "{n}" in tpl else tpl
            r = await c.get(url)
            r.raise_for_status()
            h = r.text
            m = _HTML_RE_CONCURSO.search(h)
            if not m:
                continue
            concurso = int(m.group(1))
            data = m.group(2)
            if concurso != n:
                continue
            nums = _pick_15_numbers_near(h, m.start())
            if not nums:
                continue
            e, o = histogram_even_odd(nums)
            return {"contest": concurso, "date": data, "numbers": nums,
                    "even_count": e, "odd_count": o, "source": "html"}
        except Exception:
            continue
    return None

# ----------------------------------------------------------------------
# Resolver de dados (3 nÃƒÂ­veis) + coleta
# ----------------------------------------------------------------------


async def _get_latest() -> Dict[str, Any]:
    key = "latest"
    cached = _cache_get(_caixa_cache, key, CAIXA_TTL_SEC)
    if cached:
        return cached

    if PREFER_MIRROR:
        m = await _mirror_get_latest()
        if m:
            _cache_put(_caixa_cache, key, m)
            return m

    j = await _json_get_latest()
    if j:
        _cache_put(_caixa_cache, key, j)
        return j

    m = await _mirror_get_latest()
    if m:
        _cache_put(_caixa_cache, key, m)
        return m

    h = await _html_get_latest()
    if h:
        _cache_put(_caixa_cache, key, h)
        return h

    return {"contest": 0, "date": "", "numbers": [], "source": "none"}


async def _get_concurso(n: int) -> Optional[Dict[str, Any]]:
    key = f"c:{n}"
    cached = _cache_get(_caixa_cache, key, CAIXA_TTL_SEC)
    if cached:
        return cached

    if PREFER_MIRROR:
        m = await _mirror_get_concurso(n)
        if m:
            _cache_put(_caixa_cache, key, m)
            return m

    j = await _json_get_concurso(n)
    if j:
        _cache_put(_caixa_cache, key, j)
        return j

    m = await _mirror_get_concurso(n)
    if m:
        _cache_put(_caixa_cache, key, m)
        return m

    h = await _html_get_concurso(n)
    if h:
        _cache_put(_caixa_cache, key, h)
        return h

    return None


async def collect_last_n(limit: int) -> List[dict]:
    """Coleta os ÃƒÂºltimos N concursos com logging detalhado"""
    logger.info(f"[COLETA] Iniciando coleta dos ÃƒÂºltimos {limit} concursos")

    try:
        latest = await _get_latest()
        last_n = int(latest.get("contest") or 0)

        if last_n <= 0:
            logger.warning("[COLETA] Nenhum concurso encontrado")
            return []

        logger.info(f"[COLETA] ÃƒÅ¡ltimo concurso: {last_n}")

        out: List[dict] = []
        n = last_n
        request_count = 0

        while n >= 1 and len(out) < limit:
            request_count += 1
            if request_count % 10 == 0:
                logger.debug(
                    f"[COLETA] Progresso: {len(out)}/{limit} concursos")

            d = await _get_concurso(n)
            if d:
                out.append(d)
            else:
                logger.warning(f"[COLETA] Concurso {n} nÃƒÂ£o encontrado")

            n -= 1

            # Pausa para nÃƒÂ£o sobrecarregar
            if request_count % 20 == 0:
                await asyncio.sleep(0.1)

        logger.info(f"[COLETA] Coleta concluÃƒÂ­da: {len(out)} concursos obtidos")
        logger.debug(
            f"[COLETA] Concursos coletados: {[d['contest'] for d in out[:5]]}...")

        return out[:limit]

    except Exception as e:
        logger.error(f"[COLETA] Erro na coleta: {str(e)}", exc_info=True)
        return []


async def collect_by_date(start: Optional[dt.date], end: Optional[dt.date], max_fetch: int = 400) -> List[dict]:
    """Coleta concursos por perÃƒÂ­odo com logging"""
    logger.info(f"[COLETA-PERIODO] Coletando de {start} a {end}")

    try:
        latest = await _get_latest()
        last_n = int(latest.get("contest") or 0)

        if last_n <= 0:
            logger.warning("[COLETA-PERIODO] Nenhum concurso base encontrado")
            return []

        results: List[dict] = []
        fetched = 0
        n = last_n

        logger.debug(f"[COLETA-PERIODO] Iniciando do concurso {last_n}")

        while n >= 1 and fetched < max_fetch:
            fetched += 1

            if fetched % 50 == 0:
                logger.debug(
                    f"[COLETA-PERIODO] {fetched} requests, {len(results)} concursos vÃƒÂ¡lidos")

            d = await _get_concurso(n)
            n -= 1

            if not d:
                continue

            dd = parse_draw_date(d.get("date") or "")
            if dd is None:
                logger.debug(
                    f"[COLETA-PERIODO] Data invÃƒÂ¡lida no concurso {d.get('contest')}")
                continue

            if start and dd < start:
                if results:
                    logger.debug(
                        f"[COLETA-PERIODO] Data {dd} antes do inÃƒÂ­cio {start}, parando")
                    break
                else:
                    continue

            if end and dd > end:
                continue

            results.append(d)

            if len(results) % 10 == 0:
                logger.debug(
                    f"[COLETA-PERIODO] Adicionado concurso {d.get('contest')} - {dd}")

        results.sort(key=lambda x: int(x["contest"]), reverse=True)
        logger.info(
            f"[COLETA-PERIODO] ConcluÃƒÂ­do: {len(results)} concursos no perÃƒÂ­odo")

        return results

    except Exception as e:
        logger.error(f"[COLETA-PERIODO] Erro: {str(e)}", exc_info=True)
        return []

# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@app.get("/", response_class=JSONResponse)
@app.head("/")  # Ã¢Â¬â€¦Ã¯Â¸Â ADICIONE ESTA LINHA!
async def root():
    return {
        "message": "Lotofacil API estÃƒÂ¡ online!",
        "version": APP_VERSION,
        "docs": "/docs",
        "examples": {
            "lotofacil": "/lotofacil?limit=10",
            "stats": "/stats?limit=60&hi=12&lo=3",
            "parity": "/parity?window=3m&even=8&odd=7",
        },
    }


@app.head("/")
async def root_head():
    return {}


@app.get("/health", response_class=JSONResponse)
async def health():
    return {"status": "ok", "app": "Lotofacil API", "version": APP_VERSION}


@app.get("/ready", response_class=JSONResponse)
async def ready():
    try:
        latest = await _get_latest()
        ok = bool(latest and latest.get("contest"))
        return {"status": "ok" if ok else "warn", "http": True, "latest_contest": latest.get("contest", 0)}
    except Exception as e:
        return {"status": "fail", "http": False, "error": str(e)}


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(limit: int = Query(10, ge=1, le=200), force: bool = False):
    cache = None if force else _agg_get("lotofacil", limit=limit)
    if cache:
        data = cache.copy()
        ts = data.pop("_ts", None)
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        return data

    draws = await collect_last_n(limit)
    payload = {
        "ok": True,
        "count": len(draws),
        "limit": limit,
        "summary": summarize_draws(draws),
        "results": draws,
        "method": "mixed",
        "source_url": "caixa|mirror|html",
        "cache_age_seconds": None,
    }
    _agg_put(payload, "lotofacil", limit=limit)
    payload["cache_age_seconds"] = 0
    return payload


@app.get("/stats", response_class=JSONResponse)
async def stats(limit: int = Query(60, ge=1, le=200),
                hi: int = Query(12, ge=0, le=15),
                lo: int = Query(3, ge=0, le=15),
                force: bool = False):
    hi = max(0, min(15, hi))
    lo = max(0, min(15-hi, lo))
    if hi + lo != 15:
        hi, lo = 12, 3

    cache = None if force else _agg_get("stats", limit=limit, hi=hi, lo=lo)
    if cache:
        data = cache.copy()
        ts = data.pop("_ts", None)
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        return data

    draws = await collect_last_n(limit)
    freqs = frequencies(draws)

    # sugestÃƒÂ£o oficial (Livro Negro)
    sugg = build_parity_suggestion(draws, 8, 7)

    payload = {
        "ok": True,
        "considered_games": len(draws),
        "limit": limit,
        "hi": hi,
        "lo": lo,
        "frequencies": freqs,

        # >>>>> AQUI ESTÃƒÂ A CORREÃƒâ€¡ÃƒÆ’O <<<<<
        "suggestion": sugg,

        "parity_pattern_example": sugg["pattern"],
        "method": "mixed",

        # >>>>>>>>>>>> carimbo em BRT <<<<<<<<<<<<
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": "caixa|mirror|html",
        "cache_age_seconds": None,
    }

    return payload


@app.get("/parity", response_class=JSONResponse)
async def parity(
    window: str = Query("3m", pattern=r"^((\d{1,2})m|all)$"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    even: int = Query(8, ge=0, le=15),
    odd: int = Query(7, ge=0, le=15),
    force: bool = False,
):
    even, odd = validate_parity(even, odd)

    cache = None if force else _agg_get(
        "parity", window=window, start=start, end=end, even=even, odd=odd)
    if cache:
        data = cache.copy()
        ts = data.pop("_ts", None)
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        return data

    if start or end:
        sd = dt.date.fromisoformat(start) if start else None
        ed = dt.date.fromisoformat(end) if end else None
    else:
        sd, ed = window_to_range(window)

    draws = await collect_by_date(sd, ed, max_fetch=400)
    last_draw = draws[0]["numbers"] if draws else []

    sugg = build_parity_suggestion(draws, even_needed=even, odd_needed=odd)

    freqs = frequencies(draws) if draws else frequencies(await collect_last_n(50))

    payload = {
        "ok": True,
        "considered_games": len(draws),
        "window": window,
        "start": sd.isoformat() if sd else None,
        "end":   ed.isoformat() if ed else None,
        "even": even, "odd": odd,
        "frequencies": freqs,
        "suggestion": sugg,
        "pattern": sugg["pattern"],
        "method": "mixed",
        # >>>>>>>>>>>> ALTERADO: carimbo em BRT <<<<<<<<<<<<
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": "caixa|mirror|html",
        "cache_age_seconds": None,
    }
    _agg_put(payload, "parity", window=window,
             start=start, end=end, even=even, odd=odd)
    payload["cache_age_seconds"] = 0
    return payload


PARITY_CANDIDATES: List[Tuple[int, int]] = [(6, 9), (7, 8), (8, 7), (9, 6)]


def summarize_derived_backtest(
    draws: List[Dict[str, Any]],
    even: int,
    odd: int,
    limit: int = 30,
    history: int = 50,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    max_index = min(limit, max(0, len(draws) - history))

    for idx in range(max_index):
        target = draws[idx]
        past_draws = draws[idx + 1:idx + 1 + history]
        official = set(target.get("numbers", []))
        suggestion = build_parity_suggestion(past_draws, even, odd)

        tested = []
        for game in suggestion.get("games", []):
            numbers = game.get("numbers", [])
            tested.append(len(set(numbers) & official))

        if not tested:
            continue

        principal_hits = tested[0]
        best_hits = max(tested)
        rows.append({
            "contest": target.get("contest"),
            "principal_hits": principal_hits,
            "best_hits": best_hits,
            "best_delta": best_hits - principal_hits,
        })

    principal = [r["principal_hits"] for r in rows]
    best = [r["best_hits"] for r in rows]

    return {
        "compared_games": len(rows),
        "history_per_game": history,
        "principal_average": round(sum(principal) / len(principal), 2) if principal else 0,
        "best_set_average": round(sum(best) / len(best), 2) if best else 0,
        "principal_11_plus": sum(1 for h in principal if h >= 11),
        "best_set_11_plus": sum(1 for h in best if h >= 11),
        "best_set_12_plus": sum(1 for h in best if h >= 12),
        "derived_improved_games": sum(1 for r in rows if r["best_delta"] > 0),
        "derived_same_games": sum(1 for r in rows if r["best_delta"] == 0),
        "total_best_delta": sum(r["best_delta"] for r in rows),
    }


def plan_rank_key(candidate: Dict[str, Any]) -> Tuple[float, int, int, int, int]:
    summary = candidate.get("summary", {})
    return (
        float(summary.get("best_set_average", 0)),
        int(summary.get("best_set_11_plus", 0)),
        int(summary.get("best_set_12_plus", 0)),
        int(summary.get("derived_improved_games", 0)),
        int(summary.get("total_best_delta", 0)),
    )


@app.get("/plan/recommended", response_class=JSONResponse)
async def plan_recommended(
    window: str = Query("3m", pattern=r"^((\d{1,2})m|all)$"),
    limit: int = Query(30, ge=5, le=80),
    history: int = Query(50, ge=20, le=120),
    force: bool = False,
):
    cache = None if force else _agg_get(
        "plan_recommended", window=window, limit=limit, history=history)
    if cache:
        data = cache.copy()
        ts = data.pop("_ts", None)
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        return data

    historical_draws = await collect_last_n(limit + history + 5)
    if len(historical_draws) < history + 1:
        return {
            "ok": False,
            "error": f"Historico insuficiente: {len(historical_draws)} concursos coletados",
            "required_minimum": history + 1,
        }

    candidates = []
    for even, odd in PARITY_CANDIDATES:
        summary = summarize_derived_backtest(
            historical_draws, even=even, odd=odd, limit=limit, history=history)
        candidates.append({
            "even": even,
            "odd": odd,
            "pattern": f"{even}-{odd}",
            "summary": summary,
        })

    ranked = sorted(candidates, key=plan_rank_key, reverse=True)
    recommended = ranked[0]

    sd, ed = window_to_range(window)
    current_draws = await collect_by_date(sd, ed, max_fetch=400)
    if len(current_draws) < 20:
        current_draws = await collect_last_n(50)

    suggestion = build_parity_suggestion(
        current_draws,
        even_needed=recommended["even"],
        odd_needed=recommended["odd"],
    )

    payload = {
        "ok": True,
        "window": window,
        "recommended": recommended,
        "candidates": ranked,
        "suggestion": suggestion,
        "considered_games": len(current_draws),
        "method": "auto_parity_derived_backtest",
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        "cache_age_seconds": None,
    }
    _agg_put(payload, "plan_recommended", window=window, limit=limit, history=history)
    payload["cache_age_seconds"] = 0
    return payload

# ----------------------------------------------------------------------
# UI (com spinner, PT-BR, manifest e SW)
# ----------------------------------------------------------------------


@app.get("/app", response_class=HTMLResponse)
@app.get("/app/", response_class=HTMLResponse)
async def ui():
    html = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Lotofácil</title>
<link rel="manifest" href="/static/manifest.webmanifest?v=3">
<link rel="icon" href="/static/favicon.ico">
<meta name="theme-color" content="#0f172a">
<style>
:root{color-scheme:dark;--bg:#0f172a;--panel:#111827;--panel2:#0b1220;--line:#243244;--text:#e5e7eb;--muted:#94a3b8;--green:#22c55e;--green-bg:#12351f;--red:#ef4444;--red-bg:#3a1218;--yellow:#facc15}
*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}.wrap{width:min(1120px,100%);margin:0 auto;padding:24px 16px 40px}.topbar{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:18px}h1{font-size:28px;line-height:1.1;margin:0;font-weight:800}.subtitle{margin-top:6px;color:var(--muted);font-size:14px}.status{color:var(--muted);font-size:13px;text-align:right}.grid{display:grid;grid-template-columns:minmax(300px,360px) minmax(0,1fr);gap:16px;align-items:start}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;min-width:0}.card+.card{margin-top:16px}.title{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px;font-weight:800;min-width:0}.pill{border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);font-size:12px;font-weight:700;white-space:nowrap}.form{display:grid;gap:14px}.field{display:grid;gap:6px;min-width:0}label{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase}input,select,button{width:100%;min-height:42px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px 10px;font:inherit}button{cursor:pointer;background:#0e7490;border-color:#0891b2;font-weight:800}button:disabled{opacity:.65;cursor:wait}.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}.metric{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px;min-width:0}.metric span{display:block;color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase}.metric strong{display:block;margin-top:4px;font-size:18px}.balls{display:grid;grid-template-columns:repeat(auto-fill,minmax(46px,46px));gap:10px;align-items:center}.ball{width:46px;height:46px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;border:2px solid var(--line)}.ball.even{background:var(--green-bg);border-color:var(--green);color:#dcfce7}.ball.odd{background:var(--red-bg);border-color:var(--red);color:#fee2e2}.ball.hit{border-color:var(--yellow);box-shadow:0 0 0 3px rgba(250,204,21,.22),0 0 18px rgba(250,204,21,.2);color:#fff7cc}.result-line{color:var(--muted);font-size:14px;line-height:1.5}.result-line strong{color:var(--text)}.legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px;color:var(--muted);font-size:12px}.legend-item{display:flex;gap:6px;align-items:center}.dot{width:12px;height:12px;border-radius:50%;border:2px solid var(--line)}.dot.even{background:var(--green-bg);border-color:var(--green)}.dot.odd{background:var(--red-bg);border-color:var(--red)}.dot.hit{background:#3f3105;border-color:var(--yellow)}.error{color:#fecaca}@media(max-width:768px){.wrap{padding:16px 10px 28px}.topbar{display:block;margin-bottom:12px}h1{font-size:24px}.subtitle{font-size:13px;line-height:1.35}.status{text-align:left;margin-top:10px}.grid{display:block}.card{width:100%;padding:14px;border-radius:8px}.card+.card,main .card{margin-top:12px}.title{font-size:15px;line-height:1.25;align-items:flex-start}.pill{font-size:11px;padding:4px 8px}.form{gap:12px}.split{grid-template-columns:1fr;gap:12px}.summary{grid-template-columns:1fr;gap:8px}.metric{padding:9px 10px}.metric strong{font-size:17px}input,select,button{min-height:48px;font-size:16px;padding:11px 12px}button{width:100%;min-height:50px}.balls{grid-template-columns:repeat(auto-fill,40px);gap:8px}.ball{width:40px;height:40px;font-size:14px;border-width:2px}.result-line{font-size:13px}.legend{gap:10px}}@media(min-width:769px) and (max-width:980px){.grid{grid-template-columns:320px minmax(0,1fr)}.balls{grid-template-columns:repeat(auto-fill,44px)}.ball{width:44px;height:44px}}@media(max-width:380px){.wrap{padding-left:8px;padding-right:8px}.card{padding:12px}.balls{grid-template-columns:repeat(auto-fill,40px);gap:7px}.ball{width:40px;height:40px;font-size:13px}.title{font-size:14px}}
</style>
<style>
/* Visual polish for simulation, badges, and official-history cards. */
.main-stack{display:flex;flex-direction:column;gap:16px}
.main-stack>.card{margin-top:0}
.simulation-main{background:linear-gradient(180deg,#122033 0%,var(--panel) 100%);border-color:#334155}
.simulation-main.win{border-color:rgba(34,197,94,.65);box-shadow:0 0 0 1px rgba(34,197,94,.12),0 16px 36px rgba(0,0,0,.22)}
.simulation-main.neutral{border-color:var(--line)}
.simulation-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}
.hit-display{font-size:30px;line-height:1.05;font-weight:900;letter-spacing:0;margin:8px 0 12px}
.sim-badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:800;border:1px solid var(--line);color:var(--text);background:#172033}
.badge-green{background:rgba(34,197,94,.12);border-color:rgba(34,197,94,.55);color:#bbf7d0}
.badge-blue{background:rgba(56,189,248,.12);border-color:rgba(56,189,248,.55);color:#bae6fd}
.badge-yellow{background:rgba(250,204,21,.13);border-color:rgba(250,204,21,.6);color:#fef3c7}
.badge-neutral{background:#172033;border-color:var(--line);color:var(--muted)}
.sim-legend{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
button:disabled{opacity:.6;cursor:not-allowed}
.history-list{display:grid;gap:10px}
.history-card{display:grid;grid-template-columns:110px minmax(0,1fr) 64px;gap:12px;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px}
.history-meta{display:grid;gap:3px}.history-contest{font-weight:800}.history-date{color:var(--muted);font-size:12px}
.history-numbers{display:flex;gap:5px;flex-wrap:wrap}.ball.small{width:28px;height:28px;font-size:12px;border-width:1px}
.history-pattern{justify-self:center}
.derived-list{display:grid;gap:10px}.derived-card{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px;display:grid;gap:8px}.derived-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.derived-title{font-weight:800}.derived-meta{display:flex;gap:6px;flex-wrap:wrap}.exclusion-line{margin-top:12px;color:var(--muted);font-size:13px}
.play-plan{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px;display:grid;gap:10px}.play-plan strong{font-size:20px}.play-plan-actions{display:flex;gap:8px;flex-wrap:wrap}.rank-list{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.loading-text{color:var(--muted);font-size:13px}
@media(max-width:768px){.main-stack{gap:12px}.simulation-main{order:1}.lab-section{order:2}.derived-section{order:3}.official-card{order:4}.history-section{order:5}.simulation-head{display:block}.hit-display{font-size:26px}.sim-badges{gap:6px}.sim-legend{margin-top:12px}.history-card{grid-template-columns:1fr;gap:8px;padding:12px}.history-pattern{justify-self:start}.history-numbers{gap:4px}.ball.small{width:26px;height:26px;font-size:11px}}
@media(min-width:769px) and (max-width:980px){.history-card{grid-template-columns:96px minmax(0,1fr) 58px}}
@media(max-width:380px){.hit-display{font-size:24px}.ball.small{width:25px;height:25px;font-size:10px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar"><div><h1>Lotofácil</h1><div class="subtitle">Robô com seleção automática de paridade e backtest do último concurso.</div></div><div id="requestStatus" class="status">Aguardando dados</div></div>
  <div class="grid">
    <aside>
      <div class="card"><div class="title">Configuração automática <span id="currentPattern" class="pill">Auto</span></div><div class="form"><div class="field"><label for="selWindow">Janela</label><select id="selWindow"><option value="1m">1 mês</option><option value="3m" selected>3 meses</option><option value="6m">6 meses</option><option value="all">Tudo</option></select></div><button id="btnRefresh" type="button">Atualizar</button></div><div class="summary"><div class="metric"><span>Jogos</span><strong id="gamesMetric">-</strong></div><div class="metric"><span>Paridade</span><strong id="patternMetric">Auto</strong></div><div class="metric"><span>Acertos</span><strong id="hitsMetric">-</strong></div></div></div>
    </aside>
    <main class="main-stack">
      <div id="simulationCard" class="card simulation-main neutral"><div class="simulation-head"><div class="title">Simulação automática</div><span id="contestBadge" class="badge badge-blue">Último concurso</span></div><div id="autoResult" class="result-line"><div class="loading-text">Executando backtest...</div></div><div class="legend sim-legend"><div class="legend-item"><span class="dot even"></span>Par</div><div class="legend-item"><span class="dot odd"></span>Ímpar</div><div class="legend-item"><span class="dot hit"></span>Acerto</div></div></div>
      <div class="card lab-section"><div class="title">Plano recomendado <span id="labStatus" class="badge badge-neutral">Aguardando</span></div><div id="labContent" class="loading-text">O plano será definido após gerar os jogos.</div></div>
      <div class="card derived-section"><div class="title">Jogos <span id="derivedStatus" class="badge badge-neutral">-</span></div><div id="derivedList" class="derived-list"><div class="loading-text">Aguardando jogos...</div></div><div id="exclusionLine" class="exclusion-line"></div></div>
      <div class="card official-card"><div class="title">Resultado oficial</div><div id="autoOfficial" class="balls"></div></div>
      <div class="card history-section"><div class="title">Últimos 10 concursos oficiais <span id="historyStatus" class="badge badge-neutral">Carregando</span></div><div id="historyList" class="history-list"><div class="loading-text">Carregando últimos concursos...</div></div></div>
    </main>
  </div>
</div>
<script>
const API = location.origin;
let activeController = null;
let requestSeq = 0;
const pad = n => String(n).padStart(2,'0');
const qs = params => new URLSearchParams(params).toString();
const el = id => document.getElementById(id);
async function api(path, params, signal){const url=`${API}${path}?${qs({...params,t:Date.now()})}`;const r=await fetch(url,{cache:'no-store',headers:{'Cache-Control':'no-cache','Accept':'application/json'},signal});const data=await r.json();if(!r.ok||data.ok===false){throw new Error(data.detail||data.error||`HTTP ${r.status}`)}return data;}
function updatePattern(E,O){const pattern=`${E}-${O}`;el('currentPattern').innerText=pattern;el('patternMetric').innerText=pattern;}
function renderBalls(targetId,numbers,hits=new Set()){el(targetId).innerHTML=(numbers||[]).map(n=>{const parityClass=n%2===0?'even':'odd';const hitClass=hits.has(n)?' hit':'';return `<div class="ball ${parityClass}${hitClass}" title="Dezena ${pad(n)}">${pad(n)}</div>`}).join('');}
function setLoading(){el('requestStatus').innerText='Escolhendo melhor paridade...';el('currentPattern').innerText='Auto';el('patternMetric').innerText='Auto';el('derivedList').innerHTML='<div class="loading-text">Gerando jogos...</div>';el('derivedStatus').innerText='...';el('exclusionLine').innerText='';el('autoOfficial').innerHTML='';el('autoResult').innerHTML='<div class="loading-text">Executando backtest...</div>';el('hitsMetric').innerText='-';el('labStatus').innerText='Definindo';el('labContent').innerHTML='<div class="loading-text">Definindo plano de jogo...</div>';el('simulationCard').className='card simulation-main neutral';el('btnRefresh').disabled=true;el('btnRefresh').innerText='Atualizando...';}
function setDone(){el('requestStatus').innerText='Dados atualizados';el('btnRefresh').disabled=false;el('btnRefresh').innerText='Atualizar';}
function setError(message){el('requestStatus').innerText='Erro ao atualizar';el('autoResult').innerHTML=`<span class="error">${message}</span>`;el('btnRefresh').disabled=false;el('btnRefresh').innerText='Atualizar';}
function renderInlineBalls(numbers,hits=new Set()){return (numbers||[]).map(n=>`<span class="ball small ${n%2===0?'even':'odd'}${hits.has(n)?' hit':''}">${pad(n)}</span>`).join('');}
function renderDerivedGames(games=[]){const list=games.slice(0,6);el('derivedStatus').innerText=`${list.length} jogos`;el('derivedList').innerHTML=list.map(g=>`<div class="derived-card"><div class="derived-head"><div class="derived-title">Jogo ${g.index}</div><div class="derived-meta"><span class="badge badge-blue">${g.pattern}</span></div></div><div class="history-numbers">${renderInlineBalls(g.numbers)}</div></div>`).join('')||'<div class="loading-text">Nenhum jogo disponível.</div>';el('exclusionLine').innerText='';}
function renderPlan(E,O,games=[],summary={}){const qtd=Math.min(games.length||6,6);el('labStatus').innerText='Pronto';el('labContent').innerHTML=`<div class="play-plan"><strong>Jogar o conjunto completo</strong><div class="play-plan-actions"><span class="badge badge-green">${qtd} jogos recomendados</span><span class="badge badge-blue">Paridade ${E}-${O}</span></div><div class="loading-text">Use os jogos listados em “Jogos”.</div></div>`;}
function renderHistory(draws){el('historyList').innerHTML=(draws||[]).map(d=>{const nums=d.numbers||[];const even=d.even_count??nums.filter(n=>n%2===0).length;const odd=d.odd_count??nums.filter(n=>n%2===1).length;const balls=nums.map(n=>`<span class="ball small ${n%2===0?'even':'odd'}">${pad(n)}</span>`).join('');return `<div class="history-card"><div class="history-meta"><div class="history-contest">Concurso ${d.contest}</div><div class="history-date">${d.date||'-'}</div></div><div class="history-numbers">${balls}</div><div class="history-pattern badge badge-blue">${even}-${odd}</div></div>`}).join('');}
async function loadHistory(){el('historyStatus').innerText='Carregando';el('historyList').innerHTML='<div class="loading-text">Carregando últimos concursos...</div>';try{const data=await api('/lotofacil',{limit:10});renderHistory(data.results||[]);el('historyStatus').innerText=`${data.count||0} jogos`;}catch(err){el('historyStatus').innerText='Erro';el('historyList').innerHTML=`<span class="error">${err.message||'Falha ao carregar histórico'}</span>`;}}
async function loadAll(force=false){const seq=++requestSeq;if(activeController)activeController.abort();activeController=new AbortController();const signal=activeController.signal;const w=el('selWindow').value;setLoading();try{const p=await api('/plan/recommended',{window:w,limit:30,history:50,...(force?{force:true}:{})},signal);if(seq!==requestSeq)return;const rec=p.recommended||{};const E=rec.even;const O=rec.odd;updatePattern(E,O);renderDerivedGames(p.suggestion.games||[]);el('gamesMetric').innerText=p.considered_games??'-';el('autoResult').innerHTML='<div class="loading-text">Executando backtest...</div>';const backtestPath=`/backtest/latest?even=${E}&odd=${O}`;console.log('[APP] Backtest:',backtestPath);const b=await api('/backtest/latest',{even:E,odd:O},signal);if(seq!==requestSeq)return;const hits=new Set(b.hits||[]);const didWin=(b.hits_count||0)>=11;const shownGames=b.games_tested||p.suggestion.games||[];renderBalls('autoOfficial',b.official,hits);renderDerivedGames(shownGames);el('simulationCard').className=`card simulation-main ${didWin?'win':'neutral'}`;el('contestBadge').className='badge badge-blue';el('contestBadge').innerText=`Concurso ${b.contest}`;el('hitsMetric').innerText=b.hits_count;el('patternMetric').innerText=b.pattern;el('currentPattern').innerText=b.pattern;el('autoResult').innerHTML=`<div class="hit-display">${didWin?'&#9989;':'&bull;'} ${b.hits_count} acertos</div><div class="sim-badges"><span class="badge ${didWin?'badge-green':'badge-neutral'}">${b.hits_count} acertos</span><span class="badge badge-blue">Paridade ${b.pattern}</span><span class="badge badge-yellow">Concurso ${b.contest}</span></div>`;renderPlan(E,O,shownGames,rec.summary||{});setDone();}catch(err){if(err.name==='AbortError')return;setError(err.message||'Falha inesperada');}}
document.addEventListener('DOMContentLoaded',()=>{el('selWindow').addEventListener('change',()=>loadAll(false));el('btnRefresh').addEventListener('click',()=>loadAll(true));loadAll(false);loadHistory();});
</script>
</body>
</html>
"""
    return HTMLResponse(
        html.replace("{APP_VERSION}", APP_VERSION),
        headers={
            "Cache-Control": "no-store",
            "Content-Type": "text/html; charset=utf-8",
        }
    )

@app.get("/simulate", response_class=JSONResponse)
async def simulate(
    contest: int = Query(..., ge=1),
    even: int = Query(8, ge=0, le=15),
    odd: int = Query(7, ge=0, le=15),
):
    """
    SimulaÃƒÂ§ÃƒÂ£o histÃƒÂ³rica:
    - Gera a combinaÃƒÂ§ÃƒÂ£o que teria sido sugerida ATÃƒâ€° a data do concurso informado
    - Compara com o resultado real
    """

    even, odd = validate_parity(even, odd)

    # --------------------------------------------------
    # 1. Buscar o concurso alvo
    # --------------------------------------------------
    all_draws = await collect_last_n(500)  # margem grande de seguranÃƒÂ§a

    target = next((d for d in all_draws if d["contest"] == contest), None)
    if not target:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "Concurso nÃƒÂ£o encontrado"}
        )

    target_date = parse_draw_date(target["date"])
    target_numbers = target["numbers"]

    # --------------------------------------------------
    # 2. HistÃƒÂ³rico SOMENTE ANTES do concurso alvo
    # --------------------------------------------------
    past_draws = [
        d for d in all_draws
        if parse_draw_date(d["date"]) and parse_draw_date(d["date"]) < target_date
    ]

    if len(past_draws) < 20:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "HistÃƒÂ³rico insuficiente para simulaÃƒÂ§ÃƒÂ£o"}
        )

    # --------------------------------------------------
    # 3. Gerar sugestÃƒÂ£o COMO SE FOSSE NAQUELA DATA
    # --------------------------------------------------
    sugg = build_parity_suggestion(
        past_draws,
        even_needed=even,
        odd_needed=odd
    )

    combo = sugg.get("combo", [])

    # --------------------------------------------------
    # 4. ComparaÃƒÂ§ÃƒÂ£o (acertos)
    # --------------------------------------------------
    hits = sorted(set(combo) & set(target_numbers))

    # --------------------------------------------------
    # 5. Retorno
    # --------------------------------------------------
    return {
        "ok": True,
        "contest": contest,
        "date": target_date,
        "suggested_at_time": combo,
        "official_result": target_numbers,
        "hits": hits,
        "hits_count": len(hits),
        "pattern": sugg.get("pattern"),
        "method": "historical_simulation",
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
    }


@app.get("/backtest/latest")
async def backtest_latest(
    even: int = Query(8, ge=0, le=15),  # <-- ESTA LINHA JÃƒÂ ESTÃƒÂ OK
    odd: int = Query(7, ge=0, le=15)    # <-- ESTA LINHA JÃƒÂ ESTÃƒÂ OK
):
    """
    Backtest automÃƒÂ¡tico CORRETO com paridade configurÃƒÂ¡vel.
    """
    even, odd = validate_parity(even, odd)
    logger.info(f"[BACKTEST] Iniciando com paridade {even}-{odd}")

    try:
        # 1. Buscar o ÃƒÂºltimo concurso
        latest = await _get_latest()
        latest_contest = int(latest.get("contest") or 0)

        logger.info(
            f"[BACKTEST] ÃƒÅ¡ltimo concurso identificado: {latest_contest}")

        if latest_contest <= 1:
            logger.warning("[BACKTEST] Concurso insuficiente para anÃƒÂ¡lise")
            return {
                "ok": False,
                "error": "NÃƒÂ£o hÃƒÂ¡ concurso suficiente para backtest"
            }

        # 2. Buscar resultado oficial do ÃƒÂºltimo concurso
        logger.debug(f"[BACKTEST] Buscando dados do concurso {latest_contest}")
        latest_draw = await _get_concurso(latest_contest)

        if not latest_draw:
            logger.error(
                f"[BACKTEST] Concurso {latest_contest} nÃƒÂ£o encontrado")
            return {
                "ok": False,
                "error": "NÃƒÂ£o foi possÃƒÂ­vel obter o ÃƒÂºltimo concurso"
            }

        official_numbers = latest_draw.get("numbers", [])
        latest_date = parse_draw_date(latest_draw.get("date", ""))

        logger.info(f"[BACKTEST] Concurso {latest_contest} em {latest_date}")
        logger.debug(
            f"[BACKTEST] Resultado oficial: {sorted(official_numbers)}")

        # 3. Coletar SOMENTE concursos ANTERIORES
        logger.info("[BACKTEST] Coletando concursos ANTERIORES...")

        previous_draws = []
        contest_to_check = latest_contest - 1
        max_attempts = 150

        while contest_to_check >= 1 and len(previous_draws) < 50 and max_attempts > 0:
            d = await _get_concurso(contest_to_check)
            if d:
                previous_draws.append(d)

            contest_to_check -= 1
            max_attempts -= 1

            if max_attempts % 20 == 0:
                await asyncio.sleep(0.1)

        logger.info(
            f"[BACKTEST] {len(previous_draws)} concursos anteriores coletados")

        if len(previous_draws) < 20:
            logger.warning(
                f"[BACKTEST] HistÃƒÂ³rico insuficiente: {len(previous_draws)} concursos")
            return {
                "ok": False,
                "error": f"HistÃƒÂ³rico insuficiente: apenas {len(previous_draws)} concursos anteriores"
            }

        # 4. Gerar sugestÃƒÂ£o com a paridade SELECIONADA pelo usuÃƒÂ¡rio
        logger.info(
            f"[BACKTEST] Executando Livro Negro com paridade {even}-{odd}")
        suggestion = build_parity_suggestion(
            previous_draws,
            even_needed=even,
            odd_needed=odd
        )

        suggested_numbers = suggestion.get("combo", [])

        if len(suggested_numbers) != 15:
            logger.error(
                f"[BACKTEST] SugestÃƒÂ£o incompleta: {len(suggested_numbers)} nÃƒÂºmeros")
            return {
                "ok": False,
                "error": "SugestÃƒÂ£o incompleta gerada"
            }

        logger.debug(
            f"[BACKTEST] SugestÃƒÂ£o gerada: {sorted(suggested_numbers)}")

        # 5. Calcular acertos
        hits = sorted(set(suggested_numbers) & set(official_numbers))
        derived_results = []
        for game in suggestion.get("games", []):
            game_numbers = game.get("numbers", [])
            game_hits = sorted(set(game_numbers) & set(official_numbers))
            derived_results.append({
                **game,
                "hits": game_hits,
                "hits_count": len(game_hits),
            })

        logger.info(
            f"[BACKTEST] Resultado: {len(hits)} acertos no concurso {latest_contest}")

        return {
            "ok": True,
            "contest": latest_contest,
            "contest_date": latest_date.isoformat() if latest_date else None,
            "suggested": suggested_numbers,
            "official": official_numbers,
            "hits": hits,
            "hits_count": len(hits),
            "games_tested": derived_results,
            "best_game": max(derived_results, key=lambda g: g["hits_count"]) if derived_results else None,
            "exclusions": suggestion.get("exclusions"),
            "pattern": suggestion.get("pattern", ""),
            "parity": suggestion.get("parity", {}),
            "valid": suggestion.get("valid", False),
            "rules": suggestion.get("rules", {}),
            "method": "backtest_real",
            "historical_draws_used": len(previous_draws),
            "oldest_draw_used": previous_draws[-1]["contest"] if previous_draws else None,
            "user_config": {
                "even": even,
                "odd": odd
            },
            "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        }

    except Exception as e:
        logger.error(f"[BACKTEST] Erro crÃƒÂ­tico: {str(e)}", exc_info=True)
        return {
            "ok": False,
            "error": f"Erro interno: {str(e)}"
        }


def compare_strategy_summary(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    hits = [r[key]["hits_count"] for r in rows]
    if not hits:
        return {
            "average_hits": 0,
            "max_hits": 0,
            "min_hits": 0,
            "games_11_plus": 0,
            "games_12_plus": 0,
            "games_13_plus": 0,
        }

    return {
        "average_hits": round(sum(hits) / len(hits), 2),
        "max_hits": max(hits),
        "min_hits": min(hits),
        "games_11_plus": sum(1 for h in hits if h >= 11),
        "games_12_plus": sum(1 for h in hits if h >= 12),
        "games_13_plus": sum(1 for h in hits if h >= 13),
    }


@app.get("/backtest/compare")
async def backtest_compare(
    limit: int = Query(30, ge=1, le=100),
    even: int = Query(7, ge=0, le=15),
    odd: int = Query(8, ge=0, le=15),
    history: int = Query(50, ge=20, le=150),
):
    """
    Compara a estrategia antiga contra a estrategia atual em concursos passados.

    Para cada concurso testado, usa apenas concursos anteriores como historico,
    evitando vazamento de resultado futuro.
    """
    even, odd = validate_parity(even, odd)
    needed_draws = limit + history + 5
    draws = await collect_last_n(needed_draws)

    if len(draws) < history + 1:
        return {
            "ok": False,
            "error": f"Historico insuficiente: {len(draws)} concursos coletados",
            "required_minimum": history + 1,
        }

    rows: List[Dict[str, Any]] = []
    max_index = min(limit, len(draws) - history)

    for idx in range(max_index):
        target = draws[idx]
        past_draws = draws[idx + 1:idx + 1 + history]
        official = sorted(target.get("numbers", []))

        legacy_suggestion = build_parity_suggestion_legacy(
            past_draws,
            even_needed=even,
            odd_needed=odd,
        )
        balanced_suggestion = build_parity_suggestion(
            past_draws,
            even_needed=even,
            odd_needed=odd,
        )

        legacy_combo = sorted(legacy_suggestion.get("combo", []))
        balanced_combo = sorted(balanced_suggestion.get("combo", []))
        legacy_hits = sorted(set(legacy_combo) & set(official))
        balanced_hits = sorted(set(balanced_combo) & set(official))

        rows.append({
            "contest": target.get("contest"),
            "date": target.get("date"),
            "official": official,
            "legacy": {
                "strategy": "legacy_frequency",
                "suggested": legacy_combo,
                "hits": legacy_hits,
                "hits_count": len(legacy_hits),
                "valid": legacy_suggestion.get("valid", False),
            },
            "balanced": {
                "strategy": "balanced_score_v2",
                "suggested": balanced_combo,
                "hits": balanced_hits,
                "hits_count": len(balanced_hits),
                "valid": balanced_suggestion.get("valid", False),
                "band_profile": balanced_suggestion.get("meta", {}).get("band_profile"),
            },
            "delta": len(balanced_hits) - len(legacy_hits),
        })

    return {
        "ok": True,
        "pattern": f"{even}-{odd}",
        "requested_limit": limit,
        "compared_games": len(rows),
        "history_per_game": history,
        "summary": {
            "legacy": compare_strategy_summary(rows, "legacy"),
            "balanced": compare_strategy_summary(rows, "balanced"),
            "balanced_better": sum(1 for r in rows if r["delta"] > 0),
            "legacy_better": sum(1 for r in rows if r["delta"] < 0),
            "same": sum(1 for r in rows if r["delta"] == 0),
            "total_delta": sum(r["delta"] for r in rows),
        },
        "results": rows,
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
    }


@app.get("/backtest/derived")
async def backtest_derived(
    limit: int = Query(30, ge=1, le=100),
    even: int = Query(7, ge=0, le=15),
    odd: int = Query(8, ge=0, le=15),
    history: int = Query(50, ge=20, le=150),
):
    even, odd = validate_parity(even, odd)
    draws = await collect_last_n(limit + history + 5)

    if len(draws) < history + 1:
        return {
            "ok": False,
            "error": f"Historico insuficiente: {len(draws)} concursos coletados",
            "required_minimum": history + 1,
        }

    rows: List[Dict[str, Any]] = []
    max_index = min(limit, len(draws) - history)
    for idx in range(max_index):
        target = draws[idx]
        past_draws = draws[idx + 1:idx + 1 + history]
        official = sorted(target.get("numbers", []))
        suggestion = build_parity_suggestion(past_draws, even, odd)
        tested = []

        for game in suggestion.get("games", []):
            numbers = game.get("numbers", [])
            hits = sorted(set(numbers) & set(official))
            tested.append({
                "index": game.get("index"),
                "role": game.get("role"),
                "numbers": numbers,
                "hits": hits,
                "hits_count": len(hits),
                "changed_out": game.get("changed_out", []),
                "changed_in": game.get("changed_in", []),
                "consecutive": game.get("consecutive"),
            })

        best = max(tested, key=lambda g: g["hits_count"]) if tested else None
        principal = tested[0] if tested else None
        rows.append({
            "contest": target.get("contest"),
            "date": target.get("date"),
            "official": official,
            "exclusions": suggestion.get("exclusions"),
            "principal": principal,
            "best_game": best,
            "games": tested,
            "best_delta": (best["hits_count"] - principal["hits_count"]) if best and principal else 0,
        })

    principal_hits = [r["principal"]["hits_count"] for r in rows if r.get("principal")]
    best_hits = [r["best_game"]["hits_count"] for r in rows if r.get("best_game")]
    improved = sum(1 for r in rows if r.get("best_delta", 0) > 0)

    return {
        "ok": True,
        "pattern": f"{even}-{odd}",
        "compared_games": len(rows),
        "history_per_game": history,
        "summary": {
            "principal_average": round(sum(principal_hits) / len(principal_hits), 2) if principal_hits else 0,
            "best_set_average": round(sum(best_hits) / len(best_hits), 2) if best_hits else 0,
            "principal_11_plus": sum(1 for h in principal_hits if h >= 11),
            "best_set_11_plus": sum(1 for h in best_hits if h >= 11),
            "best_set_12_plus": sum(1 for h in best_hits if h >= 12),
            "derived_improved_games": improved,
            "derived_same_games": sum(1 for r in rows if r.get("best_delta", 0) == 0),
            "total_best_delta": sum(r.get("best_delta", 0) for r in rows),
        },
        "results": rows,
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
    }


@app.get("/optimizer/weights")
async def optimizer_weights(
    limit: int = Query(30, ge=10, le=80),
    even: int = Query(7, ge=0, le=15),
    odd: int = Query(8, ge=0, le=15),
    history: int = Query(50, ge=20, le=120),
):
    even, odd = validate_parity(even, odd)
    draws = await collect_last_n(limit + history + 5)

    if len(draws) < history + 1:
        return {
            "ok": False,
            "error": f"Historico insuficiente: {len(draws)} concursos coletados",
            "required_minimum": history + 1,
        }

    profiles = [
        {"name": "baseline", "long": 1.0, "recent": 1.0, "recency": 1.0, "gap": 1.0, "percentage": 0.0},
        {"name": "recent_plus", "long": 0.8, "recent": 1.4, "recency": 1.2, "gap": 0.8, "percentage": 0.0},
        {"name": "long_plus", "long": 1.4, "recent": 0.8, "recency": 0.8, "gap": 1.0, "percentage": 0.0},
        {"name": "gap_soft", "long": 1.0, "recent": 1.0, "recency": 1.0, "gap": 1.8, "percentage": 0.0},
        {"name": "percentage_soft", "long": 1.0, "recent": 1.0, "recency": 1.0, "gap": 1.0, "percentage": 2.0},
        {"name": "balanced_recent", "long": 1.1, "recent": 1.25, "recency": 1.0, "gap": 1.0, "percentage": 0.5},
    ]

    results = []
    max_index = min(limit, len(draws) - history)
    for profile in profiles:
        hits_list = []
        for idx in range(max_index):
            target = draws[idx]
            past_draws = draws[idx + 1:idx + 1 + history]
            suggestion = build_weighted_suggestion(past_draws, even, odd, profile)
            hits_list.append(len(set(suggestion["combo"]) & set(target.get("numbers", []))))

        results.append({
            "profile": profile,
            "average_hits": round(sum(hits_list) / len(hits_list), 2) if hits_list else 0,
            "max_hits": max(hits_list) if hits_list else 0,
            "min_hits": min(hits_list) if hits_list else 0,
            "games_11_plus": sum(1 for h in hits_list if h >= 11),
            "games_12_plus": sum(1 for h in hits_list if h >= 12),
            "hits": hits_list,
        })

    ranked = sorted(results, key=lambda r: (r["average_hits"], r["games_11_plus"], r["max_hits"]), reverse=True)
    return {
        "ok": True,
        "pattern": f"{even}-{odd}",
        "compared_games": max_index,
        "history_per_game": history,
        "best_profile": ranked[0] if ranked else None,
        "profiles": ranked,
        "note": "Endpoint de laboratorio; nao altera a estrategia principal automaticamente.",
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
    }


@app.get("/debug/backtest")
async def debug_backtest():
    """Endpoint para diagnÃƒÂ³stico do backtest"""
    try:
        # Testar cada componente
        latest = await _get_latest()
        latest_contest = latest.get("contest", 0)

        if latest_contest:
            latest_draw = await _get_concurso(latest_contest)
            historical = await collect_last_n(50)

            return {
                "ok": True,
                "latest_contest": latest_contest,
                "latest_draw_exists": bool(latest_draw),
                "historical_count": len(historical),
                "historical_contests": [d.get("contest") for d in historical[:5]],
                "timestamp": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S")
            }
        else:
            return {"ok": False, "error": "NÃƒÂ£o foi possÃƒÂ­vel obter o ÃƒÂºltimo concurso"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/backtest/history")
async def backtest_history(limit: int = Query(10, ge=1, le=50)):
    """Mostra a evoluÃƒÂ§ÃƒÂ£o das sugestÃƒÂµes ao longo do tempo"""
    # ImplementaÃƒÂ§ÃƒÂ£o que pega os ÃƒÂºltimos N concursos
    # e mostra a sugestÃƒÂ£o que seria feita para cada um
    # e quantos acertos teria dado


@app.get("/render-test")
async def render_test():
    """Teste especÃƒÂ­fico para Render"""
    import os
    return {
        "status": "ok",
        "service": "lotofacil-api",
        "port": os.getenv("PORT", "10000"),
        "python_version": os.getenv("PYTHON_VERSION", "unknown"),
        "render": True,
        "timestamp": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S")
    }


@app.get("/debug/render")
async def debug_render():
    """Endpoint especÃƒÂ­fico para debug no Render"""
    import os
    return {
        "status": "ok",
        "render": True,
        "env_vars": {k: v for k, v in os.environ.items() if "PYTHON" in k or "TIME" in k},
        "cwd": os.getcwd(),
        "files": os.listdir("."),
        "timestamp": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S")
    }


@app.on_event("shutdown")
async def _shutdown():
    await close_http()
