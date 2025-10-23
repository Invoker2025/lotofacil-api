# Lotofacil API – v6.5.1
# - Coleta resultados da Lotofácil com 3 níveis:
#     1) Mirror público (opcionalmente preferido)
#     2) JSON oficial (Portal de Loterias CAIXA)
#     3) HTML oficial (página de resultados: scraping tolerante)
# - UI simples em /app; /ready mostra latest_contest; ícones e PWA em /static.

from __future__ import annotations
import logging

import os
import re
import json
import time
import datetime as dt
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ----------------------------------------------------------------------
# Paths / versão
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
STATIC_DIR = (BASE_DIR / "static").resolve()
APP_VERSION = "6.5.1"

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


def build_parity_suggestion(draws: List[dict], even_needed: int = 8, odd_needed: int = 7) -> Dict[str, Any]:
    even_needed = max(0, min(15, even_needed))
    odd_needed = max(0, min(15-even_needed, odd_needed))
    if even_needed + odd_needed != 15:
        even_needed, odd_needed = 8, 7
    freq = frequencies(draws)
    ev = sorted([f for f in freq if f["n"] % 2 == 0],
                key=lambda x: (-x["count"], x["n"]))[:even_needed]
    od = sorted([f for f in freq if f["n"] % 2 == 1],
                key=lambda x: (-x["count"], x["n"]))[:odd_needed]
    combo = sorted([x["n"] for x in ev] + [x["n"] for x in od])
    return {
        "even": [x["n"] for x in ev],
        "odd":  [x["n"] for x in od],
        "combo": combo,
        "parity": {"even_count": even_needed, "odd_count": odd_needed},
        "pattern": f"{even_needed}-{odd_needed}",
    }


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
    latest = await _get_latest()
    last_n = int(latest.get("contest") or 0)
    if last_n <= 0:
        return []
    out: List[dict] = []
    n = last_n
    while n >= 1 and len(out) < limit:
        d = await _get_concurso(n)
        if d:
            out.append(d)
        n -= 1
    return out[:limit]


async def collect_by_date(start: Optional[dt.date], end: Optional[dt.date], max_fetch: int = 400) -> List[dict]:
    latest = await _get_latest()
    last_n = int(latest.get("contest") or 0)
    if last_n <= 0:
        return []
    results: List[dict] = []
    fetched = 0
    n = last_n
    while n >= 1 and fetched < max_fetch:
        d = await _get_concurso(n)
        fetched += 1        # cada request conta
        n -= 1
        if not d:
            continue
        dd = parse_draw_date(d.get("date") or "")
        if dd is None:
            continue
        if start and dd < start:
            if results:
                break
            else:
                continue
        if end and dd > end:
            continue
        results.append(d)
    results.sort(key=lambda x: int(x["contest"]), reverse=True)
    return results

# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@app.get("/", response_class=JSONResponse)
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
    sugg = build_parity_suggestion(draws, 8, 7)
    payload = {
        "ok": True,
        "considered_games": len(draws),
        "limit": limit,
        "hi": hi, "lo": lo,
        "frequencies": freqs,
        "suggestion": {
            "hi": [x["n"] for x in sorted(freqs, key=lambda x: (-x["count"], x["n"]))[:hi]],
            "lo": [x["n"] for x in sorted(freqs, key=lambda x: (x["count"], x["n"]))[:lo]],
            "combo": sorted(
                [x["n"]
                    for x in sorted(freqs, key=lambda x: (-x["count"], x["n"]))[:hi]]
                + [x["n"]
                    for x in sorted(freqs, key=lambda x: (x["count"], x["n"]))[:lo]]
            ),
            "pattern": "n/a",
        },
        "parity_pattern_example": sugg["pattern"],
        "method": "mixed",
        # >>>>>>>>>>>> ALTERADO: carimbo em BRT <<<<<<<<<<<<
        "updated_at": dt.datetime.now(BRT).strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": "caixa|mirror|html",
        "cache_age_seconds": None,
    }
    _agg_put(payload, "stats", limit=limit, hi=hi, lo=lo)
    payload["cache_age_seconds"] = 0
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
    sugg = build_parity_suggestion(draws, even_needed=even, odd_needed=odd)
    freqs = frequencies(draws)

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
</style>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>

<div class="wrap">
  <h2>Lotofácil</h2>

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

      <!-- Botão Atualizar com spinner -->
      <button id="btnRefresh" class="btn" type="button" onclick="loadAll(true)">
        <span id="spin" class="spin hidden" aria-hidden="true"></span>
        <span id="btnText">Atualizar</span>
      </button>
    </div>

    <div class="row" style="margin-top:10px">
      <span class="badge"><b>Atualizado</b> <span id="bdupdated">—</span></span>
      <span class="badge"><b>Janela</b> <span id="bdwindow">—</span></span>
      <span class="badge"><b>Jogos</b> <span id="bdgames">—</span></span>
      <span class="badge"><b>Concurso mais atual</b> <span id="bdlatest">—</span></span>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;">
      <div class="title">Combinação sugerida <span class="pill" id="pillParidade">8-7 (pares/ímpares)</span></div>
    </div>
    <div id="suggBalls" class="row"></div>
  </div>

  <div class="card"><div class="title">Frequência por dezena (na janela)</div><canvas id="chartFreq"></canvas></div>

  <div class="card">
    <div class="title">Amostra (10 últimos)</div>
    <table id="tbl"><thead><tr><th>Concurso</th><th>Data</th><th>Dezenas</th><th>Padrão</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="muted">Fonte: CAIXA · API pessoal · v{APP_VERSION}</div>
</div>

<script>
function pad2(n){ return String(n).padStart(2,'0'); }

// >>> NOVO: converte 'aaaa-mm-dd' para 'dd-mm-aaaa' (padrão BR)
function formatBRDate(iso){
  if(!iso) return '';
  try{
    const [y,m,d] = iso.split('-').map(Number);
    return `${pad2(d)}-${pad2(m)}-${y}`;
  }catch(e){ return iso; }
}

function formatBrDateTime(s){
  if(!s) return '';
  try{
    const [d, t='00:00:00'] = s.split(' ');
    const [dd, mm, yy] = d.split('/').map(Number);
    const [hh, mi, ss] = t.split(':').map(Number);
    return new Date(yy, (mm-1), dd, hh, mi, (ss||0));
  }catch(e){ return null; }
}
function agoBR(s){
  const dt = formatBrDateTime(s);
  if(!dt) return '';
  const diff = Math.max(0, (Date.now()-dt.getTime())/1000);
  if(diff < 60){ const v = Math.round(diff); return `há ${v} segundo${v!==1?'s':''}`; }
  if(diff < 3600){ const v = Math.round(diff/60); return `há ${v} minuto${v!==1?'s':''}`; }
  const v = Math.round(diff/3600); return `há ${v} hora${v!==1?'s':''}`;
}
function ball(n){ const c=(n%2===0)?'g':'r'; return `<div class="ball ${c}">${pad2(n)}</div>`; }
let freqChart=null;
async function j(url){ const r=await fetch(url); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }

async function loadAll(force=false){
  const spin = document.getElementById('spin');
  const btnText = document.getElementById('btnText');
  spin.classList.remove('hidden'); btnText.textContent='Atualizando...';
  try{
    const w=document.getElementById('selWindow').value;
    const start=document.getElementById('inpStart').value;
    const end=document.getElementById('inpEnd').value;
    const E=document.getElementById('inpEven').value||8;
    const O=document.getElementById('inpOdd').value||7;

    let url=`/parity?window=${w}&even=${E}&odd=${O}${force?'&force=true':''}`;
    if(start||end) url=`/parity?even=${E}&odd=${O}${start?`&start=${start}`:''}${end?`&end=${end}`:''}${force?'&force=true':''}`;

    const [p,rdy] = await Promise.all([ j(url), j('/ready') ]);
    const latest = rdy?.latest_contest ?? '—';

    const updated = p.updated_at || '—';
    const ago = updated ? ' · '+agoBR(updated) : '';
    document.getElementById('bdupdated').innerText = `${updated}${ago}`;
    const _wStart = p.start ? formatBRDate(p.start) : '—';
    const _wEnd   = p.end   ? formatBRDate(p.end)   : '—';
    document.getElementById('bdwindow').innerText = `${_wStart} → ${_wEnd}`;    
    document.getElementById('bdgames').innerText   = p.considered_games ?? '—';
    document.getElementById('bdlatest').innerText  = latest;

    const s=p.suggestion;
    document.getElementById('pillParidade').innerText = s.pattern+' (pares/ímpares)';
    document.getElementById('suggBalls').innerHTML = s.combo.map(ball).join('');

    const labels=p.frequencies.map(x=>pad2(x.n));
    const data=p.frequencies.map(x=>x.count);
    if(freqChart) freqChart.destroy();
    freqChart=new Chart(document.getElementById('chartFreq'), {
      type:'bar', data:{labels, datasets:[{label:'Frequência', data}]},
      options:{responsive:true, plugins:{legend:{display:false}}}
    });

    const lotos=await j('/lotofacil?limit=10');
    const tb=document.querySelector('#tbl tbody'); tb.innerHTML='';
    for(const r of lotos.results){
      const pad=`${r.even_count}-${r.odd_count}`;
      const nums=r.numbers.map(pad2).join(' ');
      tb.insertAdjacentHTML('beforeend', `<tr><td>${r.contest}</td><td>${r.date||''}</td><td>${nums}</td><td>${pad}</td></tr>`);
    }
  } finally {
    spin.classList.add('hidden'); btnText.textContent='Atualizar';
  }
}
loadAll(false);
</script>

<script>
if('serviceWorker' in navigator){
   navigator.serviceWorker.register('/static/sw.js?v=3').catch(()=>{});
}
</script>

</body>
</html>
"""
    return HTMLResponse(html.replace("{APP_VERSION}", APP_VERSION))


@app.on_event("shutdown")
async def _shutdown():
    await close_http()
