# v4.3.0 - Lotofacil API 🎯  "12 mais / 3 menos"
# FastAPI + Playwright (Chromium) + UI embutida
# - Parser: tenta __NEXT_DATA__ (Next.js) e faz fallback para cards do HTML
# - Scraping rápido: domcontentloaded, bloqueio de assets pesados, timeout curto
# - Cache simples em memória para páginas e agregados
# ------------------------------------------------------------------------------

from __future__ import annotations

import os
import re
import json
import time
import math
import asyncio
import datetime as dt
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from playwright.async_api import (
    async_playwright,
    Browser,
    TimeoutError as PWTimeoutError,
)

APP_VERSION = "4.3.0"

BASE_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

# ---------- Tunáveis de performance ----------
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "12000"))  # 12s
MAX_PAGES_DEFAULT = int(os.getenv("MAX_PAGES_DEFAULT", "6"))
MAX_PAGES_FORCE = int(os.getenv("MAX_PAGES_FORCE", "12"))

# cache TTLs
PAGE_TTL_SEC = int(os.getenv("PAGE_TTL_SEC", "120"))        # html de cada página
AGG_TTL_SEC = int(os.getenv("AGG_TTL_SEC", "90"))           # agregado / stats

# ---------- Estado global do navegador / cache ----------
_pw = None
_browser: Browser | None = None
_browser_ready = False

_page_cache: Dict[str, Tuple[float, str]] = {}  # url -> (ts, html)
_agg_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}   # key -> (ts, data)

# ------------------------------------------------------------------------------
# FastAPI
# ------------------------------------------------------------------------------
app = FastAPI(title="Lotofacil API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Navegador
# ------------------------------------------------------------------------------

async def ensure_browser() -> Browser:
    global _pw, _browser, _browser_ready
    if _browser and _browser_ready:
        return _browser

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    _browser_ready = True
    return _browser


async def close_browser():
    global _pw, _browser, _browser_ready
    try:
        if _browser:
            await _browser.close()
    finally:
        _browser = None
        _browser_ready = False
        if _pw:
            await _pw.stop()
            _pw = None


async def render_page(url: str, timeout_ms: int | None = None) -> str:
    """
    Abre a página de forma rápida e retorna o HTML.
    - Espera 'domcontentloaded'
    - Bloqueia imagens / CSS / fontes
    """
    tmo = timeout_ms or PW_TIMEOUT_MS
    browser = await ensure_browser()
    context = await browser.new_context()
    page = await context.new_page()

    # Bloqueia assets pesados
    async def _block(route):
        if route.request.resource_type in {"image", "font", "stylesheet", "media"}:
            return await route.abort()
        return await route.continue_()
    await context.route("**/*", _block)

    try:
        try:
            await page.goto(url, timeout=tmo, wait_until="domcontentloaded")
        except PWTimeoutError:
            # Ainda assim podemos ter HTML útil
            pass
        # espera algo mínimo no DOM
        try:
            await page.wait_for_selector("main, body", timeout=3000)
        except PWTimeoutError:
            pass

        html = await page.content()
        return html
    finally:
        await context.close()

# ------------------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------------------

def _walk(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        yield obj
        for v in obj:
            yield from _walk(v)

def _looks_like_draw(item: dict) -> bool:
    """
    Heurística para reconhecer 'um concurso' no JSON do Next.js:
    - possui id/numero/concurso
    - dezenas com 15 números
    """
    if not isinstance(item, dict):
        return False

    id_keys = ["concurso", "numero", "id"]
    concurso = None
    for k in id_keys:
        if k in item:
            try:
                concurso = int(str(item[k]).strip())
                break
            except Exception:
                pass
    if not concurso:
        return False

    # dezenas
    dz = (
        item.get("dezenas")
        or item.get("numeros")
        or item.get("resultado")
        or item.get("listaDezenas")
    )
    if not isinstance(dz, (list, tuple)) or len(dz) != 15:
        return False

    try:
        dz = [f"{int(str(x).strip()):02d}" for x in dz]
    except Exception:
        return False

    item["_parsed_concurso"] = concurso
    item["_parsed_dezenas"] = dz

    for dk in ["data", "dataSorteio", "dtSorteio", "data_concurso", "date"]:
        if dk in item:
            item["_parsed_date"] = str(item[dk])
            break

    return True


def _normalize_draws(arr: list[dict]) -> list[dict]:
    out: List[Dict[str, Any]] = []
    for it in arr:
        if _looks_like_draw(it):
            out.append(
                {
                    "contest": it["_parsed_concurso"],
                    "date": it.get("_parsed_date") or "",
                    "numbers": [int(x) for x in it["_parsed_dezenas"]],
                    "source": "next_data",
                }
            )
    out.sort(key=lambda x: int(x["contest"]), reverse=True)
    return out


def parse_from_next_data(html: str) -> list[dict]:
    m = re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S | re.I
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []

    candidates: list[list[dict]] = []
    for arr in _walk(data):
        if not isinstance(arr, list) or not arr:
            continue
        sample = [x for x in arr if _looks_like_draw(x)]
        if len(sample) >= 3:
            candidates.append(arr)

    if not candidates:
        return []
    best = max(candidates, key=len)
    return _normalize_draws(best)


def parse_from_cards(html: str) -> list[dict]:
    """
    Fallback simples: pega blocos com 15 números e tenta achar o 'Concurso 3511' por perto.
    """
    blocks = re.findall(r"(?:\D|^)((?:\d{2}[\s,;/-]+){14}\d{2})(?:\D|$)", html)
    draws = []
    seen = set()
    for blk in blocks:
        i = html.find(blk)
        around = 150
        snippet = html[max(0, i - around) : i + len(blk) + around]
        m = re.search(r"Concurso[^0-9]{0,10}(\d{3,5})", snippet, re.I)
        if not m:
            continue
        contest = int(m.group(1))
        if contest in seen:
            continue
        nums = [int(x) for x in re.findall(r"\d{2}", blk)]
        if len(nums) != 15:
            continue
        seen.add(contest)
        draws.append(
            {
                "contest": contest,
                "date": "",
                "numbers": nums,
                "source": "cards",
            }
        )
    draws.sort(key=lambda x: int(x["contest"]), reverse=True)
    return draws


def parse_concursos(html: str) -> list[dict]:
    data = parse_from_next_data(html)
    if data:
        return data
    return parse_from_cards(html)

# ------------------------------------------------------------------------------
# Coletor paginado + cache
# ------------------------------------------------------------------------------

def _page_cache_get(url: str) -> str | None:
    ent = _page_cache.get(url)
    if not ent:
        return None
    ts, html = ent
    if time.time() - ts <= PAGE_TTL_SEC:
        return html
    return None


def _page_cache_put(url: str, html: str):
    _page_cache[url] = (time.time(), html)


async def fetch_page_html(url: str, force: bool = False) -> str:
    if not force:
        cached = _page_cache_get(url)
        if cached is not None:
            return cached
    html = await render_page(url)
    _page_cache_put(url, html)
    return html


async def collect_results(limit: int, force: bool = False) -> tuple[list[dict], str]:
    """
    Retorna (lista_de_concursos, method_usado)
    """
    max_pages = MAX_PAGES_FORCE if force else MAX_PAGES_DEFAULT
    pages_by_limit = max(1, math.ceil(limit / 25))  # média ~25 por página
    max_pages = min(max_pages, pages_by_limit)

    results: List[dict] = []
    seen = set()
    method_used = None

    for page_idx in range(1, max_pages + 1):
        url = BASE_URL if page_idx == 1 else f"{BASE_URL}?page={page_idx}"
        html = await fetch_page_html(url, force=force)
        part = parse_concursos(html)
        if part:
            if method_used is None:
                method_used = part[0].get("source", "mixed")
            for it in part:
                c = int(it["contest"])
                if c in seen:
                    continue
                results.append(it)
                seen.add(c)
                if len(results) >= limit:
                    return results[:limit], (method_used or "mixed")

        # se veio vazio, não adianta continuar paginando
        if not part:
            break

    return results[:limit], (method_used or "mixed")

# ------------------------------------------------------------------------------
# Estatísticas e utilidades
# ------------------------------------------------------------------------------

def histogram_even_odd(numbers: List[int]) -> Tuple[int, int]:
    e = sum(1 for n in numbers if n % 2 == 0)
    o = 15 - e
    return e, o


def summarize_draws(draws: List[dict]) -> Dict[str, Any]:
    """
    Gera algumas estatísticas simples para /lotofacil e /stats
    """
    hist = {"7-8": 0, "8-7": 0, "outros": 0}
    total = max(1, len(draws))
    even_list, odd_list = [], []

    for d in draws:
        e, o = histogram_even_odd(d["numbers"])
        if e == 7 and o == 8:
            hist["7-8"] += 1
        elif e == 8 and o == 7:
            hist["8-7"] += 1
        else:
            hist["outros"] += 1
        even_list.append(e)
        odd_list.append(o)

    avg_even = sum(even_list) / total
    avg_odd = sum(odd_list) / total

    return {"histogram": hist, "avg_even": round(avg_even, 1), "avg_odd": round(avg_odd, 1)}


def frequencies(draws: List[dict]) -> List[Dict[str, Any]]:
    counts = {n: 0 for n in range(1, 26)}
    total = max(1, len(draws))
    for d in draws:
        for x in d["numbers"]:
            counts[x] += 1
    out = []
    for n in range(1, 26):
        pct = (counts[n] / total) * 100.0
        out.append({"n": n, "count": counts[n], "pct": round(pct, 1)})
    return out


def build_suggestion(draws: List[dict], hi: int, lo: int) -> Dict[str, Any]:
    """
    Heurística "12 mais / 3 menos" (ou o par hi/lo informado):
    - Ordena dezenas por frequência (desc)
    - Pega 'hi' do topo e 'lo' do fim, totalizando 15
    """
    hi = max(0, min(15, int(hi)))
    lo = max(0, min(15 - hi, int(lo)))
    # fallback p/ 12/3
    if hi + lo != 15:
        hi, lo = 12, 3

    # contagem
    freq = frequencies(draws)
    # ordena: mais frequentes primeiro, depois número
    freq_sorted = sorted(freq, key=lambda x: (-x["count"], x["n"]))

    hi_nums = [x["n"] for x in freq_sorted[:hi]]
    lo_nums = [x["n"] for x in sorted(freq_sorted[-lo:], key=lambda x: (x["count"], x["n"]))]

    combo = sorted([*hi_nums, *lo_nums])
    even = [n for n in combo if n % 2 == 0]
    odd = [n for n in combo if n % 2 == 1]

    pattern = f"{len(even)}-{len(odd)}"

    return {
        "hi": hi_nums,
        "lo": lo_nums,
        "combo": combo,
        "parity": {
            "even_count": len(even),
            "odd_count": len(odd),
            "even": even,
            "odd": odd,
        },
        "pattern": pattern,
    }

# ------------------------------------------------------------------------------
# Cache de agregados
# ------------------------------------------------------------------------------

def _agg_key(kind: str, **params) -> str:
    return f"{kind}:{json.dumps(params, sort_keys=True)}"


def _agg_get(kind: str, **params) -> Dict[str, Any] | None:
    ent = _agg_cache.get(_agg_key(kind, **params))
    if not ent:
        return None
    ts, payload = ent
    if time.time() - ts <= AGG_TTL_SEC:
        return payload
    return None


def _agg_put(payload: Dict[str, Any], kind: str, **params):
    _agg_cache[_agg_key(kind, **params)] = (time.time(), payload)

# ------------------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------------------

@app.get("/", response_class=JSONResponse)
async def root():
    return {
        "message": "Lotofacil API está online!",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
        "examples": {
            "debug": "/debug?page=1",
            "lotofacil": "/lotofacil?limit=60",
            "stats": "/stats?limit=60&hi=12&lo=3",
        },
    }


@app.get("/health", response_class=JSONResponse)
async def health():
    return {"status": "ok", "app": "Lotofacil API", "version": APP_VERSION}


@app.get("/ready", response_class=JSONResponse)
async def ready():
    try:
        await ensure_browser()
        return {"status": "ok", "chromium": True}
    except Exception:
        return {"status": "fail", "chromium": False}


@app.get("/debug", response_class=JSONResponse)
async def debug(page: int = Query(1, ge=1), force: bool = False):
    url = BASE_URL if page == 1 else f"{BASE_URL}?page={page}"
    html = await fetch_page_html(url, force=force)
    snippet = html[:1000]
    return {"page": page, "len": len(html), "snippet": snippet}


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(
    limit: int = Query(60, ge=1, le=500),
    force: bool = False,
):
    # cache
    cache = None if force else _agg_get("lotofacil", limit=limit)
    if cache:
        cache_age = int(time.time() - cache["_ts"])
        data = cache.copy()
        data["cache_age_seconds"] = cache_age
        return data

    draws, method = await collect_results(limit=limit, force=force)
    # resumo por paridade
    for d in draws:
        e, o = histogram_even_odd(d["numbers"])
        d["even_count"], d["odd_count"] = e, o

    data = {
        "ok": True,
        "count": len(draws),
        "limit": limit,
        "summary": summarize_draws(draws),
        "results": draws,
        "method": method,
        "source_url": BASE_URL,
        "cache_age_seconds": None,
    }
    data["_ts"] = time.time()
    _agg_put(data, "lotofacil", limit=limit)
    data.pop("_ts", None)
    return data


@app.get("/stats", response_class=JSONResponse)
async def stats(
    limit: int = Query(60, ge=1, le=500),
    hi: int = Query(12, ge=0, le=15),
    lo: int = Query(3, ge=0, le=15),
    force: bool = False,
):
    # normaliza hi/lo p/ total 15
    hi = max(0, min(15, hi))
    lo = max(0, min(15 - hi, lo))
    if hi + lo != 15:
        hi, lo = 12, 3

    # cache
    cache = None if force else _agg_get("stats", limit=limit, hi=hi, lo=lo)
    if cache:
        cache_age = int(time.time() - cache["_ts"])
        data = cache.copy()
        data["cache_age_seconds"] = cache_age
        return data

    draws, method = await collect_results(limit=limit, force=force)
    freqs = frequencies(draws)
    sugg = build_suggestion(draws, hi=hi, lo=lo)

    data = {
        "ok": True,
        "considered_games": len(draws),
        "limit": limit,
        "hi": hi,
        "lo": lo,
        "frequencies": freqs,
        "suggestion": sugg,
        "pattern": sugg["pattern"],
        "method": method,
        "updated_at": dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "source_url": BASE_URL,
        "cache_age_seconds": None,
    }
    data["_ts"] = time.time()
    _agg_put(data, "stats", limit=limit, hi=hi, lo=lo)
    data.pop("_ts", None)
    return data


# ---------------------------- UI /app -----------------------------------------
@app.get("/app", response_class=HTMLResponse)
async def ui():
    # UI simples (tailwind + chart.js) — mesma estrutura que você já usa
    return HTMLResponse(
        """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lotofácil – 12 mais / 3 menos</title>
  <style>
    :root { color-scheme: dark; }
    body { background:#0f172a; color:#e2e8f0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto; }
    .wrap { max-width: 1024px; margin: 24px auto; padding: 0 16px; }
    .card { background:#0b1220; border:1px solid #1e293b; border-radius:12px; padding:16px; margin:16px 0; }
    .title { font-weight:700; font-size:18px; margin-bottom:8px; }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    input, button { background:#0b1220; color:#e2e8f0; border:1px solid #1e293b; border-radius:10px; padding:10px 12px; }
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
  <h2>Lotofácil – 12 mais / 3 menos</h2>

  <div class="card">
    <div class="row">
      <div>Concursos (limit)</div>
      <input id="inpLimit" type="number" value="60" min="1" max="500" />
      <div>Mais (hi)</div>
      <input id="inpHi" type="number" value="12" min="0" max="15" />
      <div>Menos (lo)</div>
      <input id="inpLo" type="number" value="3" min="0" max="15" />
      <label class="row"><input id="chkForce" type="checkbox" />&nbsp;Forçar atualização</label>
      <button onclick="loadAll()">Atualizar</button>
    </div>
    <div class="muted" id="meta"></div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between;">
      <div class="title">Combinação sugerida <span class="pill" id="pillDezenas">15 dezenas</span> <span class="pill" id="pillParidade">7-8 (pares/ímpares)</span></div>
    </div>
    <div id="suggBalls" class="row"></div>
  </div>

  <div class="card">
    <div class="title">Frequência por dezena</div>
    <canvas id="chartFreq"></canvas>
  </div>

  <div class="card">
    <div class="title">Últimos concursos</div>
    <table id="tbl">
      <thead><tr><th>Concurso</th><th>Data</th><th>Dezenas</th><th>Padrão</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="muted">Fonte: Sorte Online · API pessoal · v""" + APP_VERSION + """</div>
</div>

<script>
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  return await res.json();
}

function ball(n, good=true) {
  const cls = good ? 'g' : 'r';
  return `<div class="ball ${cls}">${String(n).padStart(2,'0')}</div>`;
}

let freqChart = null;

async function loadAll() {
  const L = document.getElementById('inpLimit').value || 60;
  const HI = document.getElementById('inpHi').value || 12;
  const LO = document.getElementById('inpLo').value || 3;
  const force = document.getElementById('chkForce').checked ? '&force=true' : '';

  const stats = await fetchJSON(`/stats?limit=${L}&hi=${HI}&lo=${LO}${force}`);
  const lotos = await fetchJSON(`/lotofacil?limit=${L}${force}`);

  document.getElementById('meta').innerText =
    `método: ${stats.method} · Atualizado: ${stats.updated_at} · jogos considerados: ${stats.considered_games}`;

  // Sugerida
  const s = stats.suggestion;
  document.getElementById('pillDezenas').innerText = '15 dezenas';
  document.getElementById('pillParidade').innerText = s.pattern + ' (pares/ímpares)';
  let html = '';
  const good = new Set(s.hi);
  const bad = new Set(s.lo);
  for (const n of s.combo) html += ball(n, good.has(n));
  document.getElementById('suggBalls').innerHTML = html;

  // Frequências
  const labels = stats.frequencies.map(x => String(x.n).padStart(2,'0'));
  const data = stats.frequencies.map(x => x.count);
  if (freqChart) freqChart.destroy();
  const ctx = document.getElementById('chartFreq');
  freqChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Frequência', data }] },
    options: { responsive:true, plugins:{legend:{display:false}} }
  });

  // Tabela
  const tb = document.querySelector('#tbl tbody');
  tb.innerHTML = '';
  for (const r of lotos.results.slice(0, 10)) {
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


# ------------------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    # inicializa preguiçoso, mas garante chromium no /ready
    pass


@app.on_event("shutdown")
async def _shutdown():
    await close_browser()

# ------------------------------------------------------------------------------
# Fim
# ------------------------------------------------------------------------------
