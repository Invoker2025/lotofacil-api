# v6.2.0 - Lotofacil API 🎯 (fonte: CAIXA)
# - Fonte oficial CAIXA (JSON)
# - Coleta robusta com:
#     * headers iguais ao site (X-Requested-With, Accept-Language, etc.)
#     * anti-cache por query (_=timestamp)
#     * fallback de hosts (servicebus2 + loterias)
#     * fallback de rota (?concurso=N) e também /N
#     * serialização das requisições específicas + backoff curto
#     * validação forte: somente 15 dezenas únicas (1..25) por concurso
# - Endpoints:
#     /ready  /lotofacil  /stats  /parity  /app
# - /parity: janela 1m, 3m, 1y ou custom (start/end ISO YYYY-MM-DD)
# - UI simples (Chart.js) em /app

from __future__ import annotations

import os
import re
import json
import time
import datetime as dt
from typing import Any, Dict, List, Tuple, Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
APP_VERSION = "6.2.0"

CAIXA_HOSTS = [
    "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil",
    "https://loterias.caixa.gov.br/portaldeloterias/api/lotofacil",
]

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))     # segs
# reqs por leva (apenas para "últimos N")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
AGG_TTL_SEC = int(os.getenv("AGG_TTL_SEC", "120"))     # cache dos agregados
CAIXA_TTL_SEC = int(os.getenv("CAIXA_TTL_SEC", "120")
                    )   # cache das respostas da CAIXA

# ------------------------------------------------------------------------------
# Estado global
# ------------------------------------------------------------------------------
_http: httpx.AsyncClient | None = None
_caixa_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # key->(ts,json)
_agg_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # key->(ts,payload)

# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = FastAPI(title="Lotofacil API", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# HTTP client
# ------------------------------------------------------------------------------


async def ensure_http() -> httpx.AsyncClient:
    """Cria um client httpx com headers muito próximos do site oficial."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "accept": "application/json, text/plain, */*",
                "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "x-requested-with": "XMLHttpRequest",
                "referer": "https://loterias.caixa.gov.br/wps/portal/loterias/landing/lotofacil",
                "origin": "https://loterias.caixa.gov.br",
                "pragma": "no-cache",
                "cache-control": "no-cache",
            },
        )
    return _http


async def close_http():
    global _http
    try:
        if _http is not None:
            await _http.aclose()
    finally:
        _http = None

# ------------------------------------------------------------------------------
# Helpers de cache
# ------------------------------------------------------------------------------


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

# ------------------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------------------


async def _sleep_ms(ms: int):
    import asyncio
    await asyncio.sleep(ms / 1000.0)


def parse_draw_date(s: str) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)  # 17/10/2025
    if m:
        d, M, y = m.groups()
        d, M, y = int(d), int(M), int(y)
        if y < 100:
            y += 2000
        try:
            return dt.date(y, M, d)
        except Exception:
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)        # 2025-10-17
    if m:
        y, M, d = map(int, m.groups())
        try:
            return dt.date(y, M, d)
        except Exception:
            return None
    return None


def window_to_range(window: str) -> Tuple[Optional[dt.date], Optional[dt.date]]:
    """
    Aceita: 'Xm' (1–12) ou 'all'. (Opcional: '1y' tratado como 12m.)
    """
    today = dt.date.today()

    # alias opcional: '1y' -> '12m'
    if window == "1y":
        window = "12m"

    # 'Xm' (meses)
    m = re.fullmatch(r"(\d{1,2})m", window)
    if m:
        months = int(m.group(1))
        if months < 1:
            months = 1
        if months > 12:
            months = 12

        # andar 'months' meses para trás com segurança (sem estourar dia)
        year = today.year
        month = today.month - months
        while month <= 0:
            month += 12
            year -= 1
        # usa min(day, 28) para evitar datas inválidas (fev, etc.)
        start = dt.date(year, month, min(today.day, 28))
        return start, today

    # tudo
    if window == "all":
        return None, today

    # fallback: 3m
    return today - dt.timedelta(days=93), today


def valid_15_unique(nums: List[int]) -> bool:
    if len(nums) != 15:
        return False
    s = set(nums)
    if len(s) != 15:
        return False
    for n in s:
        if n < 1 or n > 25:
            return False
    return True


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
    return {"histogram": hist, "avg_even": round(sum(evens)/total, 1), "avg_odd": round(sum(odds)/total, 1)}


def frequencies(draws: List[dict]) -> List[Dict[str, Any]]:
    counts = {n: 0 for n in range(1, 26)}
    total = max(1, len(draws))
    for d in draws:
        nums = d.get("numbers") or []
        if not valid_15_unique(nums):
            continue
        for x in nums:
            counts[x] += 1
    return [{"n": n, "count": counts[n], "pct": round((counts[n]/total)*100.0, 1)} for n in range(1, 26)]


def build_parity_suggestion(draws: List[dict], even_needed: int = 8, odd_needed: int = 7) -> Dict[str, Any]:
    even_needed = max(0, min(15, even_needed))
    odd_needed = max(0, min(15 - even_needed, odd_needed))
    if even_needed + odd_needed != 15:
        even_needed, odd_needed = 8, 7
    freq = frequencies(draws)
    ev = sorted([f for f in freq if f["n"] % 2 == 0],
                key=lambda x: (-x["count"], x["n"]))[:even_needed]
    od = sorted([f for f in freq if f["n"] % 2 == 1],
                key=lambda x: (-x["count"], x["n"]))[:odd_needed]
    chosen_even = [x["n"] for x in ev]
    chosen_odd = [x["n"] for x in od]
    combo = sorted(chosen_even + chosen_odd)
    return {
        "even": chosen_even, "odd": chosen_odd, "combo": combo,
        "parity": {"even_count": even_needed, "odd_count": odd_needed},
        "pattern": f"{even_needed}-{odd_needed}",
    }

# ------------------------------------------------------------------------------
# CAIXA API – normalização + chamadas robustas
# ------------------------------------------------------------------------------


def _normalize_json(j: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normaliza UM concurso e valida:
      - 15 dezenas únicas ∈ [1..25]
    """
    try:
        numero = int(j.get("numero"))
        dezenas = j.get("listaDezenas") or j.get("dezenas") or []
        nums = [int(str(x)) for x in dezenas]
        if not valid_15_unique(nums):
            return None
        date = str(j.get("dataApuracao") or j.get("data") or "")
        e, o = histogram_even_odd(nums)
        return {"contest": numero, "date": date, "numbers": nums,
                "even_count": e, "odd_count": o, "source": "caixa"}
    except Exception:
        return None


async def _caixa_get_latest() -> Dict[str, Any]:
    cache_key = "latest"
    cached = _cache_get(_caixa_cache, cache_key, CAIXA_TTL_SEC)
    if cached:
        return cached
    client = await ensure_http()
    # pegamos do primeiro host; se falhar, tentamos o segundo
    for base in CAIXA_HOSTS:
        try:
            r = await client.get(base, params={"_": int(time.time()*1000)}, follow_redirects=True)
            r.raise_for_status()
            j = r.json()
            data = _normalize_json(j)
            if not data:  # ainda que falhe a normalização, devolvemos ao menos o número
                data = {"contest": int(j.get("numero", 0)), "date": j.get(
                    "dataApuracao") or "", "numbers": [], "source": "caixa"}
            _cache_put(_caixa_cache, cache_key, data)
            return data
        except httpx.HTTPError:
            continue
    # se tudo falhar, devolvemos um placeholder
    return {"contest": 0, "date": "", "numbers": [], "source": "caixa"}


async def _caixa_get_concurso(n: int) -> Optional[Dict[str, Any]]:
    """
    Busca 1 concurso específico, tentando:
      - ?concurso=N com anti-cache
      - /N com anti-cache
    Valida que j['numero'] == N (senão a API devolveu o último; descartamos).
    Serializado + backoff curto para driblar caches intermediários.
    """
    cache_key = f"c:{n}"
    cached = _cache_get(_caixa_cache, cache_key, CAIXA_TTL_SEC)
    if cached:
        return cached

    client = await ensure_http()
    ts = int(time.time() * 1000)
    variants = []
    for base in CAIXA_HOSTS:
        variants.append({"url": base,      "params": {"concurso": n, "_": ts}})
        variants.append({"url": f"{base}/{n}", "params": {"_": ts}})

    for item in variants:
        try:
            r = await client.get(item["url"], params=item["params"], follow_redirects=True)
            if r.status_code == 404:
                await _sleep_ms(60)
                continue
            r.raise_for_status()
            j = r.json()
            if int(j.get("numero", 0)) != int(n):
                # API ignorou o parâmetro -> tenta próxima variante
                await _sleep_ms(60)
                continue
            data = _normalize_json(j)
            if not data:
                await _sleep_ms(60)
                continue
            _cache_put(_caixa_cache, cache_key, data)
            return data
        except httpx.HTTPError:
            await _sleep_ms(80)
            continue
    return None

# ------------------------------------------------------------------------------
# Coleta (últimos N) e por janela
# ------------------------------------------------------------------------------


async def collect_last_n(limit: int) -> list[dict]:
    """Coleta os últimos `limit` concursos válidos (15 dezenas)."""
    latest = await _caixa_get_latest()
    last_n = int(latest.get("contest") or 0)
    if last_n <= 0:
        return []
    results: List[dict] = []
    n = last_n
    while n >= 1 and len(results) < limit:
        # serialização de N em N (evita a API confundir resposta)
        got = await _caixa_get_concurso(n)
        if got:
            results.append(got)
        n -= 1
    return results[:limit]


async def collect_by_date(start: Optional[dt.date], end: Optional[dt.date], max_fetch: int = 400) -> list[dict]:
    """
    Busca concursos decrescendo até cobrir a janela [start, end].
    Como o endpoint específico sofre com cache, mantemos serializado.
    """
    latest = await _caixa_get_latest()
    last_n = int(latest.get("contest") or 0)
    if last_n <= 0:
        return []

    results: List[dict] = []
    fetched = 0
    n = last_n

    while n >= 1 and fetched < max_fetch:
        d = await _caixa_get_concurso(n)
        fetched += 1
        n -= 1
        if not d:
            continue
        dd = parse_draw_date(d.get("date") or "")
        if dd is None:
            continue
        if start and dd < start:
            # já cruzamos o início da janela; se já temos algo, podemos encerrar
            if results:
                break
            else:
                continue
        if end and dd > end:
            continue
        results.append(d)

    # ordenar desc por concurso
    results.sort(key=lambda x: int(x["contest"]), reverse=True)
    return results

# ------------------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------------------


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
        latest = await _caixa_get_latest()
        ok = bool(latest and latest.get("contest"))
        return {"status": "ok" if ok else "warn", "http": True, "latest_contest": latest.get("contest")}
    except Exception as e:
        return {"status": "fail", "http": False, "error": str(e)}


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(limit: int = Query(10, ge=1, le=200), force: bool = False):
    cache = None if force else _agg_get("lotofacil", limit=limit)
    if cache:
        data = cache.copy()
        ts = data.get("_ts")
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        data.pop("_ts", None)
        return data

    draws = await collect_last_n(limit)
    payload = {
        "ok": True,
        "count": len(draws),
        "limit": limit,
        "summary": summarize_draws(draws),
        "results": draws,
        "method": "caixa",
        "source_url": CAIXA_HOSTS[0],
        "cache_age_seconds": None,
    }
    _agg_put(payload, "lotofacil", limit=limit)
    payload["cache_age_seconds"] = 0
    return payload


@app.get("/stats", response_class=JSONResponse)
async def stats(
    limit: int = Query(60, ge=1, le=200),
    hi: int = Query(12, ge=0, le=15),
    lo: int = Query(3, ge=0, le=15),
    force: bool = False,
):
    hi = max(0, min(15, hi))
    lo = max(0, min(15 - hi, lo))
    if hi + lo != 15:
        hi, lo = 12, 3

    cache = None if force else _agg_get("stats", limit=limit, hi=hi, lo=lo)
    if cache:
        data = cache.copy()
        ts = data.get("_ts")
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        data.pop("_ts", None)
        return data

    draws = await collect_last_n(limit)
    freqs = frequencies(draws)
    sugg = build_parity_suggestion(draws, even_needed=8, odd_needed=7)

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
        "method": "caixa",
        "updated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": CAIXA_HOSTS[0],
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
        ts = data.get("_ts")
        data["cache_age_seconds"] = int(time.time() - ts) if ts else None
        data.pop("_ts", None)
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
        "method": "caixa",
        "updated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": CAIXA_HOSTS[0],
        "cache_age_seconds": None,
    }
    _agg_put(payload, "parity", window=window,
             start=start, end=end, even=even, odd=odd)
    payload["cache_age_seconds"] = 0
    return payload

# ------------------------------------------------------------------------------
# UI simples
# ------------------------------------------------------------------------------


@app.get("/app", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(
        """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lotofácil – 8 pares / 7 ímpares</title>
  <style>
    :root { color-scheme: dark; }
    body { background:#0f172a; color:#e2e8f0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto; }
    .wrap { max-width: 1024px; margin: 24px auto; padding: 0 16px; }
    .card { background:#0b1220; border:1px solid #1e293b; border-radius:12px; padding:16px; margin:16px 0; }
    .title { font-weight:700; font-size:18px; margin-bottom:8px; }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    input, select, button { background:#0b1220; color:#e2e8f0; border:1px solid #1e293b; border-radius:10px; padding:10px 12px; }
    button { cursor:pointer; }
    .pill { border-radius:999px; padding:10px 14px; border:1px solid #1e293b; }
    .ball { width:60px; height:60px; border-radius:999px; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:18px; margin:8px; }
    .ball.g { background:#16a34a22; border:1px solid #16a34a; color:#d1fae5; }
    .ball.r { background:#ef444422; border:1px solid #ef4444; color:#fee2e2; }
    table { width:100%; border-collapse: collapse; }
    th, td { padding:10px; border-top: 1px solid #1e293b; text-align:left; }
    canvas { width:100%; height:260px; }
    .muted { color:#94a3b8; font-size:12px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="wrap">
  <h2>Lotofácil – 8 pares / 7 ímpares</h2>

  <div class="card">
    <div class="row">
      <div>Janela</div>
      <select id="selWindow"></select>
        <script>
        // preencher select com 1..12 meses + "Tudo"
        (function fillWindowOptions(){
            const sel = document.getElementById('selWindow');
            for (let m = 1; m <= 12; m++) {
            const opt = document.createElement('option');
            opt.value = `${m}m`;
            opt.textContent = m === 1 ? '1 mês' : `${m} meses`;
            if (m === 3) opt.selected = true; // padrão 3m
            sel.appendChild(opt);
            }
            const all = document.createElement('option');
            all.value = 'all';
            all.textContent = 'Tudo';
            sel.appendChild(all);
        })();
        </script>


      <div>Custom:</div>
      <input id="inpStart" type="date" />
      <input id="inpEnd" type="date" />

      <div>Pares</div>
      <input id="inpEven" type="number" value="8" min="0" max="15" />
      <div>Ímpares</div>
      <input id="inpOdd" type="number" value="7" min="0" max="15" />
      <label class="row"><input id="chkForce" type="checkbox" />&nbsp;Forçar atualização</label>
      <button onclick="loadAll()">Atualizar</button>
    </div>
    <div class="muted" id="meta"></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;">
      <div class="title">Combinação sugerida <span class="pill" id="pillParidade">8-7 (pares/ímpares)</span></div>
    </div>
    <div id="suggBalls" class="row"></div>
  </div>

  <div class="card">
    <div class="title">Frequência por dezena (na janela)</div>
    <canvas id="chartFreq"></canvas>
  </div>

  <div class="card">
    <div class="title">Amostra (10 últimos)</div>
    <table id="tbl">
      <thead><tr><th>Concurso</th><th>Data</th><th>Dezenas</th><th>Padrão</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="muted">Fonte: CAIXA · API pessoal · v""" + APP_VERSION + """</div>
</div>

<script>
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return await res.json();
}

function ball(n) {
  const cls = (n % 2 === 0) ? 'g' : 'r';
  return `<div class="ball ${cls}">${String(n).padStart(2,'0')}</div>`;
}

let freqChart = null;

async function loadAll() {
  const w = document.getElementById('selWindow').value;
  const start = document.getElementById('inpStart').value;
  const end = document.getElementById('inpEnd').value;
  const E = document.getElementById('inpEven').value || 8;
  const O = document.getElementById('inpOdd').value || 7;
  const force = document.getElementById('chkForce').checked ? '&force=true' : '';

  let url = `/parity?window=${w}&even=${E}&odd=${O}${force}`;
  if (start || end) {
    const s = start ? `&start=${start}` : '';
    const e = end ? `&end=${end}` : '';
    url = `/parity?even=${E}&odd=${O}${s}${e}${force}`;
  }

  // 👉 pega os dados da paridade e, em paralelo, o último concurso em /ready
  const [p, rdy] = await Promise.all([
    fetchJSON(url),
    fetchJSON('/ready')
  ]);

  // compatível com a resposta do /ready deste backend (latest_contest)
  const latestContest = rdy?.latest_contest ?? '—';

  // 👉 aqui adicionamos "concurso mais atual: XX" na mesma linha
  document.getElementById('meta').innerText =
    `método: ${p.method} · Atualizado: ${p.updated_at} · jogos considerados: ${p.considered_games} · ` +
    `janela: ${p.start || '—'} → ${p.end || '—'} · concurso mais atual: ${latestContest}`;

  // Sugerida
  const s = p.suggestion;
  document.getElementById('pillParidade').innerText = s.pattern + ' (pares/ímpares)';
  let html = '';
  for (const n of s.combo) html += ball(n);
  document.getElementById('suggBalls').innerHTML = html;

  // Frequências
  const labels = p.frequencies.map(x => String(x.n).padStart(2,'0'));
  const data = p.frequencies.map(x => x.count);
  if (freqChart) freqChart.destroy();
  const ctx = document.getElementById('chartFreq');
  freqChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Frequência', data }] },
    options: { responsive:true, plugins:{legend:{display:false}} }
  });

  // Tabela de amostra (10 últimos)
  const lotos = await fetchJSON('/lotofacil?limit=10');
  const tb = document.querySelector('#tbl tbody');
  tb.innerHTML = '';
  for (const r of lotos.results) {
    const padrao = `${r.even_count}-${r.odd_count}`;
    const nums = r.numbers.map(n => String(n).padStart(2,'0')).join(' ');
    const tr = `<tr><td>${r.contest}</td><td>${r.date||''}</td><td>${nums}</td><td>${padrao}</td></tr>`;
    tb.insertAdjacentHTML('beforeend', tr);
  }
}
loadAll();
</script>
</body>
</html>
        """
    )


@app.on_event("shutdown")
async def _shutdown():
    await close_http()
