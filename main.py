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
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
print(">>> MAIN.PY NOVO COM /SIMULATE CARREGADO <<<")

# Lotofacil API â€“ v6.5.1
# - Coleta resultados da LotofÃ¡cil com 3 nÃ­veis:
#     1) Mirror pÃºblico (opcionalmente preferido)
#     2) JSON oficial (Portal de Loterias CAIXA)
#     3) HTML oficial (pÃ¡gina de resultados: scraping tolerante)
# - UI simples em /app; /ready mostra latest_contest; Ã­cones e PWA em /static.


# ----------------------------------------------------------------------
# Paths / versÃ£o
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
APP_VERSION = "6.5.1"

# ----------------------------------------------------------------------
# ConfiguraÃ§Ã£o de Logging
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

# Desativa logs do uvicorn se quiser menos ruÃ­do
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

logger.info(f"ðŸŽ¯ Lotofacil API v{APP_VERSION} iniciando...")
# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
app = FastAPI(title="LotofÃ¡cil API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
# Sirva a pasta "static" (Ã­cones/manifest/sw)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- DIAGNÃ“STICO /static (Ãºtil pra 404) -------------------------------
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

# --- PÃ¡gina oficial (HTML) para scraping ---
CAIXA_HTML_URLS = [
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx",
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx?concurso={n}",
]

# --- Mirror pÃºblico (somente leitura) ---
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
            detail="Paridade invÃ¡lida: even + odd deve ser igual a 15"
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
    # Quentes: apareceram em 70%+ dos Ãºltimos concursos
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


def build_parity_suggestion(
    draws: List[dict],
    even_needed: int = 8,
    odd_needed: int = 7
) -> Dict[str, Any]:

    # ======================================================
    # INÃCIO DO BLOCO DE TRATAMENTO DE ERROS
    # ======================================================
    try:
        logger.debug(
            f"[LIVRO NEGRO] Iniciando sugestÃ£o com {len(draws)} concursos")

        # ------------------------------------------------------
        # ValidaÃ§Ã£o bÃ¡sica de entrada
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
                "error": "Draws insuficientes para anÃ¡lise"
            }

        # ------------------------------------------------------
        # DEBUG: Log dos primeiros concursos
        # ------------------------------------------------------
        if logger.isEnabledFor(logging.DEBUG):
            sample = [f"{d.get('contest', '?')}" for d in draws[:3]]
            logger.debug(
                f"[LIVRO NEGRO] Primeiros concursos: {', '.join(sample)}")
            logger.debug(
                f"[LIVRO NEGRO] Config: {even_needed} pares, {odd_needed} Ã­mpares")

        # ------------------------------------------------------
        # SeguranÃ§a bÃ¡sica de parÃ¢metros
        # ------------------------------------------------------
        even_needed = max(0, min(15, even_needed))
        odd_needed = max(0, min(15 - even_needed, odd_needed))
        if even_needed + odd_needed != 15:
            even_needed, odd_needed = 8, 7
            logger.info(
                f"[LIVRO NEGRO] Paridade ajustada para {even_needed}-{odd_needed}")

        # ------------------------------------------------------
        # Ãšltimo concurso (regra das repetidas)
        # ------------------------------------------------------
        last_draw = draws[0]["numbers"] if draws else []
        logger.debug(f"[LIVRO NEGRO] Ãšltimo concurso: {sorted(last_draw)}")

        # ------------------------------------------------------
        # TENDÃŠNCIA â€” Livro Negro (janela fixa = 20)
        # ------------------------------------------------------
        trend = classify_trend(draws, window=20)
        hot = trend.get("hot", [])
        warm = trend.get("warm", [])
        cold = trend.get("cold", [])

        logger.debug(f"[LIVRO NEGRO] Quentes: {sorted(hot)}")
        logger.debug(f"[LIVRO NEGRO] Mornas : {sorted(warm)}")
        logger.debug(f"[LIVRO NEGRO] Frias  : {sorted(cold)}")

        allowed = set(hot + warm)   # frias ficam FORA
        logger.debug(
            f"[LIVRO NEGRO] Dezenas permitidas ({len(allowed)}): {sorted(allowed)}")

        # ------------------------------------------------------
        # FrequÃªncia apenas das dezenas permitidas
        # ------------------------------------------------------
        freq_all = frequencies(draws)
        freq = [f for f in freq_all if f["n"] in allowed]

        if not freq or len(freq) < 15:
            logger.warning(
                f"[LIVRO NEGRO] FrequÃªncia insuficiente: {len(freq)} dezenas")
            # Fallback: usar todas as dezenas
            freq = freq_all

        # ------------------------------------------------------
        # SeleÃ§Ã£o por paridade (8 pares / 7 Ã­mpares)
        # ------------------------------------------------------
        # ðŸ”¥ EstratÃ©gia dinÃ¢mica baseada na paridade dominante
        if even_needed > odd_needed:
            # Mais pares â†’ prioriza PARES quentes, ÃMPARES mornos
            ev_pool = [f for f in freq if f["n"] % 2 == 0]
            od_pool = [f for f in freq if f["n"] % 2 == 1]

            ev = sorted(
                ev_pool, key=lambda x: (-x["count"], x["n"]))[:even_needed]
            od = sorted(od_pool, key=lambda x: (
                x["count"], x["n"]))[:odd_needed]

        elif odd_needed > even_needed:
            # Mais Ã­mpares â†’ prioriza ÃMPARES quentes, PARES mornos
            ev_pool = [f for f in freq if f["n"] % 2 == 0]
            od_pool = [f for f in freq if f["n"] % 2 == 1]

            ev = sorted(ev_pool, key=lambda x: (
                x["count"], x["n"]))[:even_needed]
            od = sorted(
                od_pool, key=lambda x: (-x["count"], x["n"]))[:odd_needed]

        else:
            # equilÃ­brio â†’ lÃ³gica atual
            ev = sorted(
                [f for f in freq if f["n"] % 2 == 0],
                key=lambda x: (-x["count"], x["n"])
            )[:even_needed]

            od = sorted(
                [f for f in freq if f["n"] % 2 == 1],
                key=lambda x: (-x["count"], x["n"])
            )[:odd_needed]

        # Verifica se temos nÃºmeros suficientes
        if len(ev) < even_needed or len(od) < odd_needed:
            logger.warning(
                f"[LIVRO NEGRO] SeleÃ§Ã£o incompleta: {len(ev)} pares, {len(od)} Ã­mpares")

            # Recalcula usando TODAS as dezenas (fallback correto)
            sorted_all = sorted(freq_all, key=lambda x: (-x["count"], x["n"]))

            ev = [f for f in sorted_all if f["n"] % 2 == 0][:even_needed]
            od = [f for f in sorted_all if f["n"] % 2 == 1][:odd_needed]

        combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])

        logger.debug(f"[LIVRO NEGRO] Combo gerado: {combo}")

        # ======================================================
        # REGRA DO LIVRO NEGRO â€” VALIDAÃ‡ÃƒO (SEM BLOQUEIO)
        # ======================================================

        valid_sum_ok = valid_sum(combo)
        valid_repeat_ok = limit_repetition(combo, last_draw, max_repeat=9)
        valid = valid_sum_ok and valid_repeat_ok

        logger.debug(
            f"[LIVRO NEGRO] ValidaÃ§Ãµes: sum_ok={valid_sum_ok}, repeat_ok={valid_repeat_ok}, valid={valid}")

        # ------------------------------------------------------
        # Retorno final
        # ------------------------------------------------------
        return {
            "even": [x["n"] for x in ev],
            "odd":  [x["n"] for x in od],
            "combo": combo,
            "parity": {
                "even_count": even_needed,
                "odd_count": odd_needed
            },
            "pattern": f"{even_needed}-{odd_needed}",
            "valid": valid,
            "rules": {
                "sum_ok": valid_sum_ok,
                "repeat_ok": valid_repeat_ok
            },
            "meta": {
                "hot_count": len(hot),
                "warm_count": len(warm),
                "cold_count": len(cold),
                "draws_analyzed": len(draws)
            }
        }

    except Exception as e:
        logger.error(f"[LIVRO NEGRO] ERRO CRÃTICO: {str(e)}", exc_info=True)
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
# Resolver de dados (3 nÃ­veis) + coleta
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
    """Coleta os Ãºltimos N concursos com logging detalhado"""
    logger.info(f"[COLETA] Iniciando coleta dos Ãºltimos {limit} concursos")

    try:
        latest = await _get_latest()
        last_n = int(latest.get("contest") or 0)

        if last_n <= 0:
            logger.warning("[COLETA] Nenhum concurso encontrado")
            return []

        logger.info(f"[COLETA] Ãšltimo concurso: {last_n}")

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
                logger.warning(f"[COLETA] Concurso {n} nÃ£o encontrado")

            n -= 1

            # Pausa para nÃ£o sobrecarregar
            if request_count % 20 == 0:
                await asyncio.sleep(0.1)

        logger.info(f"[COLETA] Coleta concluÃ­da: {len(out)} concursos obtidos")
        logger.debug(
            f"[COLETA] Concursos coletados: {[d['contest'] for d in out[:5]]}...")

        return out[:limit]

    except Exception as e:
        logger.error(f"[COLETA] Erro na coleta: {str(e)}", exc_info=True)
        return []


async def collect_by_date(start: Optional[dt.date], end: Optional[dt.date], max_fetch: int = 400) -> List[dict]:
    """Coleta concursos por perÃ­odo com logging"""
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
                    f"[COLETA-PERIODO] {fetched} requests, {len(results)} concursos vÃ¡lidos")

            d = await _get_concurso(n)
            n -= 1

            if not d:
                continue

            dd = parse_draw_date(d.get("date") or "")
            if dd is None:
                logger.debug(
                    f"[COLETA-PERIODO] Data invÃ¡lida no concurso {d.get('contest')}")
                continue

            if start and dd < start:
                if results:
                    logger.debug(
                        f"[COLETA-PERIODO] Data {dd} antes do inÃ­cio {start}, parando")
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
            f"[COLETA-PERIODO] ConcluÃ­do: {len(results)} concursos no perÃ­odo")

        return results

    except Exception as e:
        logger.error(f"[COLETA-PERIODO] Erro: {str(e)}", exc_info=True)
        return []

# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@app.get("/", response_class=JSONResponse)
@app.head("/")  # â¬…ï¸ ADICIONE ESTA LINHA!
async def root():
    return {
        "message": "Lotofacil API estÃ¡ online!",
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

    # sugestÃ£o oficial (Livro Negro)
    sugg = build_parity_suggestion(draws, 8, 7)

    payload = {
        "ok": True,
        "considered_games": len(draws),
        "limit": limit,
        "hi": hi,
        "lo": lo,
        "frequencies": freqs,

        # >>>>> AQUI ESTÃ A CORREÃ‡ÃƒO <<<<<
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

# ----------------------------------------------------------------------
# UI (com spinner, PT-BR, manifest e SW)
# ----------------------------------------------------------------------


@app.get("/app", response_class=HTMLResponse)
@app.get("/app/", response_class=HTMLResponse)
async def ui(response: Response):
    response.headers["Cache-Control"] = "no-store"

    html = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<title>LotofÃ¡cil</title>
<link rel="manifest" href="/static/manifest.webmanifest?v=3">
<link rel="icon" href="/static/favicon.ico">
<meta name="theme-color" content="#0f172a">

<style>
:root{color-scheme:dark}
body{background:#0f172a;color:#e2e8f0;font-family:system-ui}
.wrap{max-width:1024px;margin:24px auto;padding:0 16px}
.card{background:#0b1220;border:1px solid #1e293b;border-radius:12px;padding:16px;margin:16px 0}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
input,select,button{background:#0b1220;color:#e2e8f0;border:1px solid #1e293b;border-radius:8px;padding:8px}
button{cursor:pointer}
.ball{width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700}
.ball.hit{background:#15803d;border:3px solid #fbbf24}
.ball.r{background:#7f1d1d;border:1px solid #ef4444}
.ball.g{background:#14532d;border:1px solid #22c55e}
.muted{color:#94a3b8;font-size:12px}
</style>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>

<div class="wrap">
<h2>LotofÃ¡cil ðŸš¨ TESTE RENDER 01</h2>


<div class="card row">
Janela
<select id="selWindow">
<option value="1m">1 mÃªs</option>
<option value="3m" selected>3 meses</option>
<option value="6m">6 meses</option>
<option value="all">Tudo</option>
</select>

Pares <input id="inpEven" type="number" value="8" min="0" max="15">
Ãmpares <input id="inpOdd" type="number" value="7" min="0" max="15">

<button onclick="loadAll(true)">Atualizar</button>
</div>

<div class="card">
<b>CombinaÃ§Ã£o sugerida</b>
<div id="suggBalls" class="row"></div>
</div>

<div class="card">
<b>ðŸ“Œ SimulaÃ§Ã£o automÃ¡tica â€” Ãºltimo concurso</b>
<div id="parityIndicator" class="muted"></div>
<div class="row">
<div id="autoSuggested" class="row"></div>
<div id="autoOfficial" class="row"></div>
</div>
<div id="autoResult" class="muted"></div>
</div>
</div>

<script>
const API = location.origin;
let activeController = null;
let requestSeq = 0;

const pad = n => String(n).padStart(2,'0');
const qs = params => new URLSearchParams(params).toString();
const el = id => document.getElementById(id);

function readParity(){
  let E = parseInt(el('inpEven').value, 10);
  let O = parseInt(el('inpOdd').value, 10);
  if (!Number.isInteger(E)) E = 8;
  if (!Number.isInteger(O)) O = 15 - E;
  E = Math.max(0, Math.min(15, E));
  O = Math.max(0, Math.min(15, O));
  if (E + O !== 15) O = 15 - E;
  el('inpEven').value = E;
  el('inpOdd').value = O;
  return { E, O };
}

async function api(path, params, signal){
  const url = `${API}${path}?${qs({ ...params, t: Date.now() })}`;
  const r = await fetch(url, {
    cache: 'no-store',
    headers: { 'Cache-Control': 'no-cache' },
    signal
  });
  const data = await r.json();
  if (!r.ok || data.ok === false) {
    throw new Error(data.detail || data.error || `HTTP ${r.status}`);
  }
  return data;
}

async function loadAll(force=false){
  const seq = ++requestSeq;
  if (activeController) activeController.abort();
  activeController = new AbortController();
  const signal = activeController.signal;

  const { E, O } = readParity();
  const w = el('selWindow').value;

  el('suggBalls').innerHTML = '';
  el('autoSuggested').innerHTML = '';
  el('autoOfficial').innerHTML = '';
  el('autoResult').innerText = 'Recalculando...';
  el('parityIndicator').innerText = `Paridade solicitada: ${E}-${O}`;

  try {
    const p = await api('/parity', {
      window: w,
      even: E,
      odd: O,
      ...(force ? { force: true } : {})
    }, signal);
    if (seq !== requestSeq) return;

    el('suggBalls').innerHTML =
      p.suggestion.combo.map(n =>
        `<div class="ball g">${pad(n)}</div>`
      ).join('');

    const d = await api('/backtest/latest', { even: E, odd: O }, signal);
    if (seq !== requestSeq) return;

    const hit = new Set(d.hits || []);
    el('parityIndicator').innerText =
      `Paridade usada: ${d.pattern} | Concurso ${d.contest}`;

    el('autoSuggested').innerHTML =
      d.suggested.map(n =>
        `<div class="ball g ${hit.has(n) ? 'hit' : ''}">${pad(n)}</div>`
      ).join('');

    el('autoOfficial').innerHTML =
      d.official.map(n =>
        `<div class="ball r ${hit.has(n) ? 'hit' : ''}">${pad(n)}</div>`
      ).join('');

    el('autoResult').innerText =
      `${d.hits_count} acertos | Sugerida: ${d.suggested.join('-')}`;
  } catch (err) {
    if (err.name === 'AbortError') return;
    el('autoResult').innerText = `Erro: ${err.message}`;
    el('parityIndicator').innerText = `Paridade solicitada: ${E}-${O}`;
  }
}

el('inpEven').addEventListener('input', () => {
  const E = Math.max(0, Math.min(15, parseInt(el('inpEven').value, 10) || 0));
  el('inpOdd').value = 15 - E;
  loadAll(false);
});
el('inpOdd').addEventListener('input', () => {
  const O = Math.max(0, Math.min(15, parseInt(el('inpOdd').value, 10) || 0));
  el('inpEven').value = 15 - O;
  loadAll(false);
});
el('selWindow').addEventListener('change', () => loadAll(false));

loadAll(false);
</script>

</body>
</html>
"""
    return HTMLResponse(html.replace("{APP_VERSION}", APP_VERSION))


@app.get("/simulate", response_class=JSONResponse)
async def simulate(
    contest: int = Query(..., ge=1),
    even: int = Query(8, ge=0, le=15),
    odd: int = Query(7, ge=0, le=15),
):
    """
    SimulaÃ§Ã£o histÃ³rica:
    - Gera a combinaÃ§Ã£o que teria sido sugerida ATÃ‰ a data do concurso informado
    - Compara com o resultado real
    """

    even, odd = validate_parity(even, odd)

    # --------------------------------------------------
    # 1. Buscar o concurso alvo
    # --------------------------------------------------
    all_draws = await collect_last_n(500)  # margem grande de seguranÃ§a

    target = next((d for d in all_draws if d["contest"] == contest), None)
    if not target:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "Concurso nÃ£o encontrado"}
        )

    target_date = parse_draw_date(target["date"])
    target_numbers = target["numbers"]

    # --------------------------------------------------
    # 2. HistÃ³rico SOMENTE ANTES do concurso alvo
    # --------------------------------------------------
    past_draws = [
        d for d in all_draws
        if parse_draw_date(d["date"]) and parse_draw_date(d["date"]) < target_date
    ]

    if len(past_draws) < 20:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "HistÃ³rico insuficiente para simulaÃ§Ã£o"}
        )

    # --------------------------------------------------
    # 3. Gerar sugestÃ£o COMO SE FOSSE NAQUELA DATA
    # --------------------------------------------------
    sugg = build_parity_suggestion(
        past_draws,
        even_needed=even,
        odd_needed=odd
    )

    combo = sugg.get("combo", [])

    # --------------------------------------------------
    # 4. ComparaÃ§Ã£o (acertos)
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
    even: int = Query(8, ge=0, le=15),  # <-- ESTA LINHA JÃ ESTÃ OK
    odd: int = Query(7, ge=0, le=15)    # <-- ESTA LINHA JÃ ESTÃ OK
):
    """
    Backtest automÃ¡tico CORRETO com paridade configurÃ¡vel.
    """
    even, odd = validate_parity(even, odd)
    logger.info(f"[BACKTEST] Iniciando com paridade {even}-{odd}")

    try:
        # 1. Buscar o Ãºltimo concurso
        latest = await _get_latest()
        latest_contest = int(latest.get("contest") or 0)

        logger.info(
            f"[BACKTEST] Ãšltimo concurso identificado: {latest_contest}")

        if latest_contest <= 1:
            logger.warning("[BACKTEST] Concurso insuficiente para anÃ¡lise")
            return {
                "ok": False,
                "error": "NÃ£o hÃ¡ concurso suficiente para backtest"
            }

        # 2. Buscar resultado oficial do Ãºltimo concurso
        logger.debug(f"[BACKTEST] Buscando dados do concurso {latest_contest}")
        latest_draw = await _get_concurso(latest_contest)

        if not latest_draw:
            logger.error(
                f"[BACKTEST] Concurso {latest_contest} nÃ£o encontrado")
            return {
                "ok": False,
                "error": "NÃ£o foi possÃ­vel obter o Ãºltimo concurso"
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
                f"[BACKTEST] HistÃ³rico insuficiente: {len(previous_draws)} concursos")
            return {
                "ok": False,
                "error": f"HistÃ³rico insuficiente: apenas {len(previous_draws)} concursos anteriores"
            }

        # 4. Gerar sugestÃ£o com a paridade SELECIONADA pelo usuÃ¡rio
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
                f"[BACKTEST] SugestÃ£o incompleta: {len(suggested_numbers)} nÃºmeros")
            return {
                "ok": False,
                "error": "SugestÃ£o incompleta gerada"
            }

        logger.debug(
            f"[BACKTEST] SugestÃ£o gerada: {sorted(suggested_numbers)}")

        # 5. Calcular acertos
        hits = sorted(set(suggested_numbers) & set(official_numbers))

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
        logger.error(f"[BACKTEST] Erro crÃ­tico: {str(e)}", exc_info=True)
        return {
            "ok": False,
            "error": f"Erro interno: {str(e)}"
        }


@app.get("/debug/backtest")
async def debug_backtest():
    """Endpoint para diagnÃ³stico do backtest"""
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
            return {"ok": False, "error": "NÃ£o foi possÃ­vel obter o Ãºltimo concurso"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/backtest/history")
async def backtest_history(limit: int = Query(10, ge=1, le=50)):
    """Mostra a evoluÃ§Ã£o das sugestÃµes ao longo do tempo"""
    # ImplementaÃ§Ã£o que pega os Ãºltimos N concursos
    # e mostra a sugestÃ£o que seria feita para cada um
    # e quantos acertos teria dado


@app.get("/render-test")
async def render_test():
    """Teste especÃ­fico para Render"""
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
    """Endpoint especÃ­fico para debug no Render"""
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
