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
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
print(">>> MAIN.PY NOVO COM /SIMULATE CARREGADO <<<")

# Lotofacil API – v6.5.1
# - Coleta resultados da Lotofácil com 3 níveis:
#     1) Mirror público (opcionalmente preferido)
#     2) JSON oficial (Portal de Loterias CAIXA)
#     3) HTML oficial (página de resultados: scraping tolerante)
# - UI simples em /app; /ready mostra latest_contest; ícones e PWA em /static.


# ----------------------------------------------------------------------
# Paths / versão
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
APP_VERSION = "6.5.1"

# ----------------------------------------------------------------------
# Configuração de Logging
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

# Desativa logs do uvicorn se quiser menos ruído
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

logger.info(f"🎯 Lotofacil API v{APP_VERSION} iniciando...")
# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------
app = FastAPI(title="Lotofácil API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
# Sirva a pasta "static" (ícones/manifest/sw)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- DIAGNÓSTICO /static (útil pra 404) -------------------------------
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

# --- Página oficial (HTML) para scraping ---
CAIXA_HTML_URLS = [
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx",
    "https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx?concurso={n}",
]

# --- Mirror público (somente leitura) ---
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
    # Quentes: apareceram em 70%+ dos últimos concursos
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
    # INÍCIO DO BLOCO DE TRATAMENTO DE ERROS
    # ======================================================
    try:
        logger.debug(
            f"[LIVRO NEGRO] Iniciando sugestão com {len(draws)} concursos")

        # ------------------------------------------------------
        # Validação básica de entrada
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
                "error": "Draws insuficientes para análise"
            }

        # ------------------------------------------------------
        # DEBUG: Log dos primeiros concursos
        # ------------------------------------------------------
        if logger.isEnabledFor(logging.DEBUG):
            sample = [f"{d.get('contest', '?')}" for d in draws[:3]]
            logger.debug(
                f"[LIVRO NEGRO] Primeiros concursos: {', '.join(sample)}")
            logger.debug(
                f"[LIVRO NEGRO] Config: {even_needed} pares, {odd_needed} ímpares")

        # ------------------------------------------------------
        # Segurança básica de parâmetros
        # ------------------------------------------------------
        even_needed = max(0, min(15, even_needed))
        odd_needed = max(0, min(15 - even_needed, odd_needed))
        if even_needed + odd_needed != 15:
            even_needed, odd_needed = 8, 7
            logger.info(
                f"[LIVRO NEGRO] Paridade ajustada para {even_needed}-{odd_needed}")

        # ------------------------------------------------------
        # Último concurso (regra das repetidas)
        # ------------------------------------------------------
        last_draw = draws[0]["numbers"] if draws else []
        logger.debug(f"[LIVRO NEGRO] Último concurso: {sorted(last_draw)}")

        # ------------------------------------------------------
        # TENDÊNCIA — Livro Negro (janela fixa = 20)
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
        # Frequência apenas das dezenas permitidas
        # ------------------------------------------------------
        freq_all = frequencies(draws)
        freq = [f for f in freq_all if f["n"] in allowed]

        if not freq or len(freq) < 15:
            logger.warning(
                f"[LIVRO NEGRO] Frequência insuficiente: {len(freq)} dezenas")
            # Fallback: usar todas as dezenas
            freq = freq_all

        # ------------------------------------------------------
        # Seleção por paridade (8 pares / 7 ímpares)
        # ------------------------------------------------------
        ev = sorted(
            [f for f in freq if f["n"] % 2 == 0],
            key=lambda x: (-x["count"], x["n"])
        )[:even_needed]

        od = sorted(
            [f for f in freq if f["n"] % 2 == 1],
            key=lambda x: (-x["count"], x["n"])
        )[:odd_needed]

        # Verifica se temos números suficientes
        if len(ev) < even_needed or len(od) < odd_needed:
            logger.warning(
                f"[LIVRO NEGRO] Seleção incompleta: {len(ev)} pares, {len(od)} ímpares")
            # Preenche com as mais frequentes disponíveis
            ev = sorted(freq_all, key=lambda x: (-x["count"], x["n"]))
            ev = [f for f in ev if f["n"] % 2 == 0][:even_needed]
            od = [f for f in ev if f["n"] % 2 == 1][:odd_needed]

        combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])

        logger.debug(f"[LIVRO NEGRO] Combo gerado: {combo}")

        # ======================================================
        # REGRA DO LIVRO NEGRO — VALIDAÇÃO (SEM BLOQUEIO)
        # ======================================================

        valid_sum_ok = valid_sum(combo)
        valid_repeat_ok = limit_repetition(combo, last_draw, max_repeat=9)
        valid = valid_sum_ok and valid_repeat_ok

        logger.debug(
            f"[LIVRO NEGRO] Validações: sum_ok={valid_sum_ok}, repeat_ok={valid_repeat_ok}, valid={valid}")

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
        logger.error(f"[LIVRO NEGRO] ERRO CRÍTICO: {str(e)}", exc_info=True)
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
# Resolver de dados (3 níveis) + coleta
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
    """Coleta os últimos N concursos com logging detalhado"""
    logger.info(f"[COLETA] Iniciando coleta dos últimos {limit} concursos")

    try:
        latest = await _get_latest()
        last_n = int(latest.get("contest") or 0)

        if last_n <= 0:
            logger.warning("[COLETA] Nenhum concurso encontrado")
            return []

        logger.info(f"[COLETA] Último concurso: {last_n}")

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
                logger.warning(f"[COLETA] Concurso {n} não encontrado")

            n -= 1

            # Pausa para não sobrecarregar
            if request_count % 20 == 0:
                await asyncio.sleep(0.1)

        logger.info(f"[COLETA] Coleta concluída: {len(out)} concursos obtidos")
        logger.debug(
            f"[COLETA] Concursos coletados: {[d['contest'] for d in out[:5]]}...")

        return out[:limit]

    except Exception as e:
        logger.error(f"[COLETA] Erro na coleta: {str(e)}", exc_info=True)
        return []


async def collect_by_date(start: Optional[dt.date], end: Optional[dt.date], max_fetch: int = 400) -> List[dict]:
    """Coleta concursos por período com logging"""
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
                    f"[COLETA-PERIODO] {fetched} requests, {len(results)} concursos válidos")

            d = await _get_concurso(n)
            n -= 1

            if not d:
                continue

            dd = parse_draw_date(d.get("date") or "")
            if dd is None:
                logger.debug(
                    f"[COLETA-PERIODO] Data inválida no concurso {d.get('contest')}")
                continue

            if start and dd < start:
                if results:
                    logger.debug(
                        f"[COLETA-PERIODO] Data {dd} antes do início {start}, parando")
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
            f"[COLETA-PERIODO] Concluído: {len(results)} concursos no período")

        return results

    except Exception as e:
        logger.error(f"[COLETA-PERIODO] Erro: {str(e)}", exc_info=True)
        return []

# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@app.get("/", response_class=JSONResponse)
@app.head("/")  # ⬅️ ADICIONE ESTA LINHA!
async def root():
    return {
        "message": "Lotofacil API está online!",
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

    # sugestão oficial (Livro Negro)
    sugg = build_parity_suggestion(draws, 8, 7)

    payload = {
        "ok": True,
        "considered_games": len(draws),
        "limit": limit,
        "hi": hi,
        "lo": lo,
        "frequencies": freqs,

        # >>>>> AQUI ESTÁ A CORREÇÃO <<<<<
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
async def ui():

    html = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<title>Lotofácil</title>
<link rel="manifest" href="/static/manifest.webmanifest?v=3" type="application/manifest+json">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="icon" href="/static/favicon.ico">
<meta name="theme-color" content="#0f172a">

<style>
:root{ color-scheme:dark; }
body{ background:#0f172a; color:#e2e8f0; font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto; }
.wrap{ max-width:1024px; margin:24px auto; padding:0 16px; }
.card{ background:#0b1220; border:1px solid #1e293b; border-radius:12px; padding:16px; margin:16px 0; }
.title{ font-weight:700; font-size:18px; margin-bottom:8px; }
.row{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
input,select,button{ background:#0b1220; color:#e2e8f0; border:1px solid #1e293b; border-radius:10px; padding:10px 12px; }
button{ cursor:pointer; }
.pill{ border-radius:999px; padding:10px 14px; border:1px solid #1e293b; }
.ball{ width:60px; height:60px; border-radius:999px; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:18px; margin:8px; }
.ball.g{ background:#16a34a22; border:1px solid #16a34a; color:#d1fae5; }
.ball.r{ background:#ef444422; border:1px solid #ef4444; color:#fee2e2; }
table{ width:100%; border-collapse:collapse; }
th,td{ padding:10px; border-top:1px solid #1e293b; text-align:left; }
canvas{ width:100%; height:260px; }
.muted{ color:#94a3b8; font-size:12px; }
.spin{ width:16px; height:16px; border:2px solid #94a3b8; border-top-color:transparent; border-radius:999px; display:inline-block; animation:spin .8s linear infinite; margin-right:8px; vertical-align:-3px; }
.hidden{ display:none; }
@keyframes spin{ to{ transform:rotate(360deg) } }

.status-ok{ color:#22c55e; font-weight:600; }
.status-warn{ color:#facc15; font-weight:600; }
.status-bad{ color:#ef4444; }
/* Estilo para bolas com acertos */
.ball.hit {
  animation: pulse 2s infinite;
  font-weight: 900;
}

@keyframes pulse {
  0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.7); }
  70% { transform: scale(1.05); box-shadow: 0 0 0 10px rgba(251, 191, 36, 0); }
  100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0); }
}

/* Legenda */
.legend {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
}

.legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}

.legend-normal {
  background: #16a34a22;
  border: 1px solid #16a34a;
}

.legend-hit {
  background: #15803d;
  border: 2px solid #fbbf24;
}

/* Cores para status de premiação */
.status-max {
  color: #fbbf24;
  font-weight: bold;
  text-shadow: 0 0 10px rgba(251, 191, 36, 0.5);
}

.status-win {
  color: #22c55e;
  font-weight: bold;
}

.status-lose {
  color: #ef4444;
}

/* Estilo para bolas */
.ball.hit {
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.7); }
  70% { transform: scale(1.05); box-shadow: 0 0 0 10px rgba(251, 191, 36, 0); }
  100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(251, 191, 36, 0); }
}

.ball.miss {
  opacity: 0.8;
}

/* Legenda */
.legend {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #1e293b;
  font-size: 12px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
}

.legend-hit {
  background: #15803d;
  border: 2px solid #fbbf24;
}

.legend-miss {
  background: #7f1d1d;
  border: 1px solid #ef4444;
}

.legend-not-suggested {
  background: #374151;
  border: 1px solid #6b7280;
}

/* Melhorar layout do card de simulação */
#autoSuggested, #autoOfficial {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 4px;
  margin: 8px 0;
}

.ball {
  width: 50px;
  height: 50px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 16px;
  transition: all 0.3s ease;
}

.ball:hover {
  transform: scale(1.1);
  z-index: 10;
}

/* Instruções */
.instructions {
  background: #0b1220;
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 12px;
  margin: 16px 0;
  font-size: 13px;
  color: #94a3b8;
}

.instructions b {
  color: #e2e8f0;
}
</style>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>

<div class="wrap">
  <h2>Lotofácil</h2>

  <!-- ABA ATUAL -->
  <div id="tab-main">

    <div class="card">
      <div class="row">
        <div>Janela</div>
        <select id="selWindow"></select>
        <script>
        (function(){
          const sel = document.getElementById('selWindow');
          for(let m=1;m<=12;m++){
            const o=document.createElement('option');
            o.value=`${m}m`; o.textContent=m===1?'1 mês':`${m} meses`;
            if(m===3) o.selected=true;
            sel.appendChild(o);
          }
          const all=document.createElement('option'); all.value='all'; all.textContent='Tudo'; sel.appendChild(all);
        })();
        </script>

        <div>Custom:</div>
        <input id="inpStart" type="date" />
        <input id="inpEnd"   type="date" />
        <div>Pares</div><input id="inpEven" type="number" value="8" min="0" max="15" />
        <div>Ímpares</div><input id="inpOdd" type="number" value="7" min="0" max="15" />

        <button id="btnRefresh" type="button" onclick="loadAll(true)">
          <span id="spin" class="spin hidden"></span>
          <span id="btnText">Atualizar</span>
        </button>
      </div>

      <div class="row" style="margin-top:10px">
        <span><b>Atualizado</b> <span id="bdupdated">—</span></span>
        <span><b>Janela</b> <span id="bdwindow">—</span></span>
        <span><b>Jogos</b> <span id="bdgames">—</span></span>
        <span><b>Concurso mais atual</b> <span id="bdlatest">—</span></span>
      </div>
    </div>

    <div class="card">
      <div class="title">
        Combinação sugerida
        <span class="pill" id="pillParidade">—</span>
      </div>
      <div id="suggBalls" class="row"></div>
      <div id="suggStatus" class="muted" style="margin-top:8px;"></div>
    </div>

    <div class="card">
      <div class="title">Frequência por dezena (na janela)</div>
      <canvas id="chartFreq"></canvas>
    </div>
    
    <!-- Instruções sobre o jogo -->
    <div class="instructions">
    <b>Como funciona a Lotofácil:</b>
    <div style="margin-top: 6px;">
        • Você marca <b>15 números</b> entre os 25 disponíveis (01 a 25)<br>
        • <span style="color:#22c55e">Ganha com 11, 12, 13, 14 ou 15 acertos</span><br>
        • <span style="color:#facc15">⚠️ IMPORTANTE: A cada novo concurso, a sugestão SE ATUALIZA automaticamente com os dados mais recentes!</span>
        • Acima mostra: <span style="color:#fbbf24">● Acertos</span> | 
        <span style="color:#ef4444">● Erros (sugeridos)</span> | 
        <span style="color:#6b7280">● Não sugeridos</span>
    </div>
    </div>

    <!-- >>> INÍCIO: SIMULAÇÃO AUTOMÁTICA (NOVO CARD) <<< -->
    <div class="card">
      <div class="title">📌 Simulação automática — último concurso</div>
      <div class="row">
        <div style="flex:1">
          <div class="muted">Sugestão do método</div>
          <div id="autoSuggested" class="row"></div>
        </div>
        <div style="flex:1">
          <div class="muted">Resultado oficial</div>
          <div id="autoOfficial" class="row"></div>
        </div>
      </div>
      <div id="autoResult" class="muted" style="margin-top:8px;"></div>
    </div>
        <!-- >>> FIM: SIMULAÇÃO AUTOMÁTICA <<< -->

    <div class="card">
      <div class="title">Amostra (10 últimos)</div>
      <table id="tbl">
        <thead>
          <tr><th>Concurso</th><th>Data</th><th>Dezenas</th><th>Padrão</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

  </div> <!-- Fecha a div#tab-main -->

  <div class="muted">Fonte: CAIXA · API pessoal · v{APP_VERSION}</div>
</div> <!-- Fecha a div.wrap -->

<script>
// =====================
// CONFIGURAÇÃO BASE
// =====================
const API_BASE_URL = window.location.origin;
let freqChart = null;

// =====================
// UTILITÁRIOS
// =====================
function pad2(n) {
  return String(n).padStart(2, '0');
}

function ball(n) {
  const c = (n % 2 === 0) ? 'g' : 'r';
  return `<div class="ball ${c}">${pad2(n)}</div>`;
}

function safeText(id, val) {
  const el = document.getElementById(id);
  if (el) el.innerText = val;
}

function safeHtml(id, val) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = val;
}

// =====================
// API FETCH
// =====================
async function j(endpoint, optional = false) {
  const sep = endpoint.includes('?') ? '&' : '?';
  const url = `${API_BASE_URL}${endpoint}${sep}t=${Date.now()}`;

  try {
    const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!r.ok) {
      if (optional && r.status === 404) return null;
      throw new Error(await r.text());
    }
    return r.json();
  } catch (e) {
    if (!optional) console.error('[API ERROR]', e);
    throw e;
  }
}

// =====================
// GRÁFICO (FORA DO loadAll)
// =====================
function renderChart(frequencies) {
  const canvas = document.getElementById('chartFreq');
  if (!canvas) return;

  if (!Array.isArray(frequencies) || frequencies.length === 0) {
    canvas.replaceWith(
      Object.assign(document.createElement('div'), {
        className: 'muted',
        innerText: 'Frequência indisponível para a janela selecionada'
      })
    );
    return;
  }

  const labels = frequencies.map(f => pad2(f.n));
  const data = frequencies.map(f => f.count);

  if (freqChart) freqChart.destroy();

  freqChart = new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets: [{ data }] },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } }
    }
  });
}

// =====================
// TABELA
// =====================
async function loadTable() {
  try {
    const data = await j('/lotofacil?limit=10', true);
    const tbody = document.querySelector('#tbl tbody');
    if (!tbody || !data?.results) return;

    tbody.innerHTML = data.results.map(r => `
      <tr>
        <td>${r.contest}</td>
        <td>${r.date || ''}</td>
        <td>${r.numbers.map(pad2).join(' ')}</td>
        <td>${r.even_count}-${r.odd_count}</td>
      </tr>
    `).join('');
  } catch {}
}

// =====================
// LOAD PRINCIPAL
// =====================
async function loadAll(force = false) {
  const spin = document.getElementById('spin');
  const btn = document.getElementById('btnText');

  spin?.classList.remove('hidden');
  if (btn) btn.innerText = 'Atualizando...';

  try {
    const w = document.getElementById('selWindow')?.value || '3m';
    const E = document.getElementById('inpEven')?.value || 8;
    const O = document.getElementById('inpOdd')?.value || 7;

    const p = await j(`/parity?window=${w}&even=${E}&odd=${O}${force ? '&force=true' : ''}`);

    safeText('bdupdated', p.updated_at || '—');
    safeText('bdwindow', `${p.start || '—'} → ${p.end || '—'}`);
    safeText('bdgames', p.considered_games || 0);

    if (p.suggestion) {
      safeHtml('pillParidade', p.suggestion.pattern);
      safeHtml('suggBalls', p.suggestion.combo.map(ball).join(''));
    }

    renderChart(p.frequencies);
    loadTable();

  } catch (e) {
    console.error(e);
    safeHtml('suggStatus', '⚠️ Erro ao carregar dados');
  } finally {
    spin?.classList.add('hidden');
    if (btn) btn.innerText = 'Atualizar';
  }
}

// =====================
// SIMULAÇÃO AUTOMÁTICA (VERSÃO FINAL)
// =====================
async function loadAutoSim() {
  const container = document.getElementById('autoResult');
  if (!container) return;
  
  container.innerHTML = '<div class="muted">Carregando simulação...</div>';
  
  try {
    const r = await j('/backtest/latest', true);
    
    if (!r || !r.ok) {
      safeHtml('autoResult', '⚠️ Backtest indisponível');
      return;
    }

    // FUNÇÃO PARA CRIAR BOLAS COM CORES CORRETAS
    function createBall(number, isHit, isSuggested = true) {
      const padded = pad2(number);
      
      if (isHit) {
        // ACERTOU: VERDE COM BORDA DOURADA
        return `<div class="ball hit" title="Acertou: ${padded}" 
                  style="background:#15803d; border:3px solid #fbbf24; 
                         box-shadow:0 0 10px rgba(251, 191, 36, 0.5);
                         font-weight:bold;">
                  ${padded}
                </div>`;
      } else if (isSuggested) {
        // SUGERIU MAS NÃO ACERTOU: VERMELHO
        return `<div class="ball miss" title="Errou: ${padded}" 
                  style="background:#7f1d1d; border:1px solid #ef4444; color:#fee2e2;">
                  ${padded}
                </div>`;
      } else {
        // NÃO FOI SUGERIDO (apenas no resultado oficial): CINZA
        return `<div class="ball" title="${padded}" 
                  style="background:#374151; border:1px solid #6b7280; color:#d1d5db;">
                  ${padded}
                </div>`;
      }
    }

    // Calcular acertos e determinar se ganhou
    const hits = r.hits || [];
    const hitsCount = r.hits_count || 0;
    const hitSet = new Set(hits);
    const suggestedSet = new Set(r.suggested || []);
    
    // Verificar premiação
    let premio = "";
    let premioClass = "";
    if (hitsCount >= 15) {
      premio = "🏆 PRÊMIO MÁXIMO! (15 acertos)";
      premioClass = "status-max";
    } else if (hitsCount >= 14) {
      premio = "💰 GANHOU! (14 acertos)";
      premioClass = "status-win";
    } else if (hitsCount >= 13) {
      premio = "💰 GANHOU! (13 acertos)";
      premioClass = "status-win";
    } else if (hitsCount >= 12) {
      premio = "💰 GANHOU! (12 acertos)";
      premioClass = "status-win";
    } else if (hitsCount >= 11) {
      premio = "💰 GANHOU! (11 acertos)";
      premioClass = "status-win";
    } else {
      premio = "❌ Não ganhou (menos de 11 acertos)";
      premioClass = "status-lose";
    }

    // Renderizar sugestão (15 números)
    if (r.suggested && r.suggested.length === 15) {
      const suggestedHtml = r.suggested.map(n => 
        createBall(n, hitSet.has(n), true)
      ).join('');
      safeHtml('autoSuggested', suggestedHtml);
    }

    // Renderizar oficial (15 números)
    if (r.official && r.official.length === 15) {
      const officialHtml = r.official.map(n => 
        createBall(n, hitSet.has(n), suggestedSet.has(n))
      ).join('');
      safeHtml('autoOfficial', officialHtml);
    }

    // Mostrar resultado com premiação
    const dateStr = r.contest_date ? 
      r.contest_date.split('-').reverse().join('/') : 
      'Data desconhecida';
    
    // Na parte que mostra o resultado, adicione:
    safeHtml(
    'autoResult',
    `<div class="${premioClass}" style="margin-bottom:8px; font-size:14px;">
        ${premio}
    </div>
    <div style="margin-bottom:8px;">
        🎯 <b>${hitsCount} acertos</b> no concurso <b>${r.contest}</b> (${dateStr})
    </div>
    <div class="muted" style="font-size:11px; margin-bottom:12px;">
        📊 Paridade ${r.pattern || '—'} · 
        📈 Analisou ${r.historical_draws_used || 'N/A'} concursos anteriores ·
        ⚡ <span style="color:#facc15">Atualiza a cada novo sorteio!</span>
    </div>
    <div class="legend">...</div>`
    );

  } catch (e) {
    console.error('Erro no auto-sim:', e);
    safeHtml('autoResult', '⚠️ Erro ao executar backtest');
  }
}

// =====================
// INIT
// =====================
window.addEventListener('DOMContentLoaded', () => {
  loadAll(false);
  loadAutoSim();
});
</script>

</body>
</html>
"""
    return HTMLResponse(html.replace("{APP_VERSION}", APP_VERSION))


@app.get("/simulate", response_class=JSONResponse)
async def simulate(
    contest: int = Query(..., ge=1)
):
    """
    Simulação histórica:
    - Gera a combinação que teria sido sugerida ATÉ a data do concurso informado
    - Compara com o resultado real
    """

    # --------------------------------------------------
    # 1. Buscar o concurso alvo
    # --------------------------------------------------
    all_draws = await collect_last_n(500)  # margem grande de segurança

    target = next((d for d in all_draws if d["contest"] == contest), None)
    if not target:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "Concurso não encontrado"}
        )

    target_date = parse_draw_date(target["date"])
    target_numbers = target["numbers"]

    # --------------------------------------------------
    # 2. Histórico SOMENTE ANTES do concurso alvo
    # --------------------------------------------------
    past_draws = [
        d for d in all_draws
        if parse_draw_date(d["date"]) and parse_draw_date(d["date"]) < target_date
    ]

    if len(past_draws) < 20:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Histórico insuficiente para simulação"}
        )

    # --------------------------------------------------
    # 3. Gerar sugestão COMO SE FOSSE NAQUELA DATA
    # --------------------------------------------------
    sugg = build_parity_suggestion(
        past_draws,
        even_needed=8,
        odd_needed=7
    )

    combo = sugg.get("combo", [])

    # --------------------------------------------------
    # 4. Comparação (acertos)
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
async def backtest_latest():
    """
    Backtest automático CORRETO:
    Usa apenas concursos ANTERIORES ao último concurso.
    """
    logger.info("[BACKTEST] Iniciando backtest automático CORRETO")

    try:
        # 1. Buscar o último concurso
        latest = await _get_latest()
        latest_contest = int(latest.get("contest") or 0)

        logger.info(
            f"[BACKTEST] Último concurso identificado: {latest_contest}")

        if latest_contest <= 1:
            logger.warning("[BACKTEST] Concurso insuficiente para análise")
            return {
                "ok": False,
                "error": "Não há concurso suficiente para backtest"
            }

        # 2. Buscar resultado oficial do último concurso
        logger.debug(f"[BACKTEST] Buscando dados do concurso {latest_contest}")
        latest_draw = await _get_concurso(latest_contest)

        if not latest_draw:
            logger.error(
                f"[BACKTEST] Concurso {latest_contest} não encontrado")
            return {
                "ok": False,
                "error": "Não foi possível obter o último concurso"
            }

        official_numbers = latest_draw.get("numbers", [])
        latest_date = parse_draw_date(latest_draw.get("date", ""))

        logger.info(f"[BACKTEST] Concurso {latest_contest} em {latest_date}")
        logger.debug(
            f"[BACKTEST] Resultado oficial: {sorted(official_numbers)}")

        # 3. Coletar SOMENTE concursos ANTERIORES
        logger.info(
            "[BACKTEST] Coletando concursos ANTERIORES (backtest real)...")

        # Vamos buscar concursos um por um até termos pelo menos 50 anteriores
        previous_draws = []
        contest_to_check = latest_contest - 1
        max_attempts = 150  # limite de segurança

        while contest_to_check >= 1 and len(previous_draws) < 50 and max_attempts > 0:
            d = await _get_concurso(contest_to_check)
            if d:
                previous_draws.append(d)
                logger.debug(
                    f"[BACKTEST] Adicionado concurso anterior: {contest_to_check}")

            contest_to_check -= 1
            max_attempts -= 1

            # Pausa para não sobrecarregar
            if max_attempts % 20 == 0:
                await asyncio.sleep(0.1)

        logger.info(
            f"[BACKTEST] {len(previous_draws)} concursos anteriores coletados")

        if len(previous_draws) < 20:
            logger.warning(
                f"[BACKTEST] Histórico insuficiente: {len(previous_draws)} concursos")
            return {
                "ok": False,
                "error": f"Histórico insuficiente: apenas {len(previous_draws)} concursos anteriores"
            }

        # 4. Gerar sugestão baseada APENAS nos concursos anteriores
        logger.info(
            "[BACKTEST] Executando Livro Negro com dados históricos...")
        suggestion = build_parity_suggestion(
            previous_draws,
            even_needed=8,
            odd_needed=7
        )

        suggested_numbers = suggestion.get("combo", [])

        if len(suggested_numbers) != 15:
            logger.error(
                f"[BACKTEST] Sugestão incompleta: {len(suggested_numbers)} números")
            return {
                "ok": False,
                "error": "Sugestão incompleta gerada"
            }

        logger.debug(
            f"[BACKTEST] Sugestão gerada: {sorted(suggested_numbers)}")

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
            "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        }

    except Exception as e:
        logger.error(f"[BACKTEST] Erro crítico: {str(e)}", exc_info=True)
        return {
            "ok": False,
            "error": f"Erro interno: {str(e)}"
        }


@app.get("/debug/backtest")
async def debug_backtest():
    """Endpoint para diagnóstico do backtest"""
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
            return {"ok": False, "error": "Não foi possível obter o último concurso"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/backtest/history")
async def backtest_history(limit: int = Query(10, ge=1, le=50)):
    """Mostra a evolução das sugestões ao longo do tempo"""
    # Implementação que pega os últimos N concursos
    # e mostra a sugestão que seria feita para cada um
    # e quantos acertos teria dado


@app.get("/render-test")
async def render_test():
    """Teste específico para Render"""
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
    """Endpoint específico para debug no Render"""
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
