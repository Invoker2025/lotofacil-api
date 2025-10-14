# main.py
# Lotofácil API — FastAPI + Playwright + Parser + Paridade + Estatísticas + UI
# v4.0.0

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# bs4 é opcional; se não estiver instalado, caímos no parser por regex
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None  # type: ignore

APP_NAME = "Lotofacil API"
APP_VERSION = "4.0.0"
DEFAULT_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

# Cache curto (para reduzir chamadas ao site)
CACHE_TTL_SECONDS = 120
_cache: Dict[str, Any] = {"ts": 0.0, "html": None}

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via Playwright com UI e estatísticas",
    docs_url="/docs",
    redoc_url=None,
)

# --------------------- Playwright helpers ---------------------


async def render_page(url: str, timeout_ms: int = 45_000) -> str:
    """Abre Chromium headless, acessa a URL e retorna o HTML."""
    launch_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-setuid-sandbox",
    ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=launch_args)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            html = await page.content()
            return html
        finally:
            await browser.close()


async def fetch_html(force: bool = False, retries: int = 2, timeout_ms: int = 50_000) -> str:
    now = time.time()
    if not force and _cache["html"] and (now - _cache["ts"] < CACHE_TTL_SECONDS):
        return _cache["html"]
    last_exc: Optional[Exception] = None
    for _ in range(retries):
        try:
            html = await render_page(DEFAULT_URL, timeout_ms=timeout_ms)
            _cache["html"] = html
            _cache["ts"] = time.time()
            return html
        except Exception as e:
            last_exc = e
            time.sleep(0.8)
    raise last_exc or RuntimeError("Falha ao baixar HTML")

# --------------------- Parser helpers -----------------------


def _to_int(x: Any) -> Optional[int]:
    try:
        s = str(x).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


def _normalize_numbers(v: Any) -> Optional[List[int]]:
    if not isinstance(v, (list, tuple)):
        return None
    out: List[int] = []
    for item in v:
        n = _to_int(item)
        if n is None:
            return None
        out.append(n)
    if len(out) >= 15 and all(1 <= n <= 25 for n in out[:15]):
        return list(out[:15])
    return None


def _walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def _pick_first(d: Dict[str, Any], keys_like: Iterable[str]) -> Optional[Any]:
    for k, v in d.items():
        lk = k.lower()
        if any(needle in lk for needle in keys_like):
            return v
    return None


def parse_from_next_data(html: str, limit: int = 10) -> List[Dict[str, Any]]:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []
    try:
        data = json.loads(script.string)
    except Exception:
        return []

    results: List[Dict[str, Any]] = []
    seen: set = set()

    for node in _walk(data):
        if not isinstance(node, dict):
            continue

        numbers = None
        for _, v in node.items():
            cand = _normalize_numbers(v)
            if cand:
                numbers = cand
                break
        if not numbers:
            continue

        contest = None
        for k, v in node.items():
            if any(x in k.lower() for x in ("concurso", "contest", "sorteio", "draw")):
                ci = _to_int(v) if isinstance(v, (str, int)) else None
                if ci:
                    contest = ci
                    break

        date = None
        for k, v in node.items():
            if any(x in k.lower() for x in ("data", "date")):
                if isinstance(v, str) and len(v) >= 6:
                    date = v
                    break

        city = _pick_first(node, ["cidade", "city", "local"])
        uf = _pick_first(node, ["uf", "estado", "state"])

        key = (contest, tuple(numbers))
        if key in seen:
            continue
        seen.add(key)

        results.append(
            {
                "contest": contest,
                "date": date,
                "numbers": numbers,
                "city": city,
                "state": uf,
                "source": "next_data",
            }
        )

        if len(results) >= limit:
            break

    results.sort(key=lambda x: (
        x["contest"] is not None, x["contest"]), reverse=True)
    return results[:limit]


def parse_from_html_regex(html: str, limit: int = 10) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    dezena = r"(?:0?[1-9]|1\d|2[0-5])"
    sep = r"(?:\s*[,|/•\-]?\s*|</[^>]+>\s*<[^>]+>)"
    bloco = rf"({dezena}(?:{sep}{dezena}){{14}})"
    blocks = re.findall(bloco, html)

    re_concurso = re.compile(
        r"concurs(?:o|o nº|o n[oº])\s*[:#]?\s*(\d{3,5})", re.I)
    re_data = re.compile(
        r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2}|"
        r"\d{1,2}\s+de\s+[A-Za-zçãéíóú]+\s+\d{2,4})",
        re.I,
    )

    for b in blocks:
        nums = re.findall(r"\d{1,2}", b)
        cand = [_to_int(n) for n in nums]
        cand = [n for n in cand if n is not None]
        if len(cand) >= 15 and all(1 <= n <= 25 for n in cand[:15]):
            cand = cand[:15]
            idx = html.find(b)
            snippet = html[max(0, idx - 1000): idx + len(b) + 1000]
            m_conc = re_concurso.search(snippet)
            m_data = re_data.search(snippet)
            results.append(
                {
                    "contest": _to_int(m_conc.group(1)) if m_conc else None,
                    "date": m_data.group(1) if m_data else None,
                    "numbers": cand,
                    "city": None,
                    "state": None,
                    "source": "regex_html",
                }
            )
            if len(results) >= limit:
                break

    # dedup
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in results:
        key = (r["contest"], tuple(r["numbers"]))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    uniq.sort(key=lambda x: (
        x["contest"] is not None, x["contest"]), reverse=True)
    return uniq[:limit]


def parse_lotofacil(html: str, limit: int = 10) -> Dict[str, Any]:
    data = parse_from_next_data(html, limit=limit)
    method = "next_data"
    if not data:
        data = parse_from_html_regex(html, limit=limit)
        method = "regex_html"
    return {"count": len(data), "method": method, "results": data}

# ---------------------- Paridade ----------------------


def parity_info(nums: List[int]) -> Dict[str, Any]:
    even = [n for n in nums if n % 2 == 0]
    odd = [n for n in nums if n % 2 != 0]
    return {
        "even_count": len(even),
        "odd_count": len(odd),
        "even": even,
        "odd": odd,
        "pattern": f"{len(even)}-{len(odd)}",
    }

# ---------------------- Estatísticas ----------------------


def frequencies(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Frequência de 1..25 nos resultados."""
    freq = {i: 0 for i in range(1, 26)}
    for r in results:
        for n in r["numbers"]:
            if 1 <= n <= 25:
                freq[n] += 1
    total_draws = len(results)
    out = []
    for n in range(1, 26):
        c = freq[n]
        pct = (c / total_draws * 100.0) if total_draws else 0.0
        out.append({"n": n, "count": c, "pct": round(pct, 2)})
    return out


def suggest_combo(freq_list: List[Dict[str, Any]], hi: int = 12, lo: int = 3) -> Dict[str, Any]:
    """Escolhe 12 mais frequentes + 3 menos frequentes (entre os restantes)."""
    hi = max(0, min(15, hi))
    lo = max(0, min(15 - hi, lo))
    by_count_desc = sorted(freq_list, key=lambda x: (-x["count"], x["n"]))
    top_hi = [x["n"] for x in by_count_desc[:hi]]

    remaining = [x for x in freq_list if x["n"] not in top_hi]
    by_count_asc = sorted(remaining, key=lambda x: (x["count"], x["n"]))
    low_lo = [x["n"] for x in by_count_asc[:lo]]

    combo = sorted(top_hi + low_lo)
    info = parity_info(combo)
    return {"hi": sorted(top_hi), "lo": sorted(low_lo), "combo": combo, "parity": info}

# ------------------------- Rotas API --------------------------


@app.get("/", response_class=JSONResponse)
async def root():
    return {
        "message": "Lotofacil API está online!",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
        "examples": {"app": "/app", "lotofacil": "/lotofacil?limit=10", "stats": "/stats?limit=50&hi=12&lo=3"},
    }


@app.head("/")
async def root_head():
    return Response(status_code=200)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    return {"status": "ok", "app": APP_NAME, "version": APP_VERSION}


@app.head("/health")
async def health_head():
    return Response(status_code=200)


@app.get("/ready", response_class=JSONResponse)
async def ready_check():
    try:
        html = await render_page("https://example.com", timeout_ms=10_000)
        ok = bool(html and "<html" in html.lower())
        return {"status": "ok" if ok else "error", "chromium": ok}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": f"{type(e).__name__}: {e}"})


@app.get("/debug", response_class=JSONResponse)
async def debug():
    try:
        html = await fetch_html(force=True, retries=2, timeout_ms=45_000)
        return {"len": len(html), "snippet": html[:600].replace("\n", " ")}
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout no debug: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro no debug: {type(e).__name__}: {str(e)}"})


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(
    limit: int = Query(default=10, ge=1, le=50),
    pattern: Optional[str] = Query(default=None, pattern=r"^\d{1,2}-\d{1,2}$"),
    min_even: Optional[int] = Query(default=None, ge=0, le=15),
    max_even: Optional[int] = Query(default=None, ge=0, le=15),
    force: bool = Query(default=False),
):
    """Lista os últimos concursos (com paridade e filtros)."""
    try:
        html = await fetch_html(force=force, retries=2, timeout_ms=60_000)
        parsed = parse_lotofacil(html, limit=limit)

        items = []
        for r in parsed["results"]:
            items.append({**r, **parity_info(r["numbers"])})

        if pattern:
            try:
                want_even, want_odd = (int(x) for x in pattern.split("-"))
                items = [x for x in items if x["even_count"] ==
                         want_even and x["odd_count"] == want_odd]
            except Exception:
                pass
        if min_even is not None:
            items = [x for x in items if x["even_count"] >= min_even]
        if max_even is not None:
            items = [x for x in items if x["even_count"] <= max_even]

        hist: Dict[str, int] = {}
        total_even = total_odd = 0
        for x in items:
            hist[x["pattern"]] = hist.get(x["pattern"], 0) + 1
            total_even += x["even_count"]
            total_odd += x["odd_count"]
        n = max(1, len(items))
        summary = {
            "histogram": dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0]))),
            "avg_even": round(total_even / n, 2),
            "avg_odd": round(total_odd / n, 2),
        }

        return {
            "ok": True,
            "count": len(items),
            "limit": limit,
            "summary": summary,
            "results": items,
            "method": parsed["method"],
            "source_url": DEFAULT_URL,
            "cache_age_seconds": round(time.time() - _cache["ts"], 2) if _cache["html"] else None,
        }

    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout na lotofacil: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"})


@app.get("/stats", response_class=JSONResponse)
async def stats(
    limit: int = Query(default=60, ge=15, le=120,
                       description="Qtde de concursos a considerar"),
    hi: int = Query(default=12, ge=0, le=15,
                    description="Qtd de dezenas mais frequentes"),
    lo: int = Query(default=3, ge=0, le=15,
                    description="Qtd de dezenas menos frequentes (entre as restantes)"),
    force: bool = Query(default=False),
):
    """Estatísticas de frequência + sugestão 12 mais / 3 menos (configurável)."""
    try:
        html = await fetch_html(force=force, retries=2, timeout_ms=60_000)
        parsed = parse_lotofacil(html, limit=limit)
        res = parsed["results"]
        freq_list = frequencies(res)
        suggest = suggest_combo(freq_list, hi=hi, lo=lo)
        updated = datetime.now(timezone.utc).astimezone().strftime(
            "%d/%m/%Y %H:%M:%S")
        return {
            "ok": True,
            "considered_games": len(res),
            "limit": limit,
            "hi": hi,
            "lo": lo,
            "frequencies": freq_list,
            "suggestion": suggest,
            "method": parsed["method"],
            "updated_at": updated,
            "source_url": DEFAULT_URL,
            "cache_age_seconds": round(time.time() - _cache["ts"], 2) if _cache["html"] else None,
        }
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout em /stats: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro em /stats: {type(e).__name__}: {str(e)}"})


@app.exception_handler(404)
async def not_found(_, __):
    return PlainTextResponse("Not Found. Veja /app, /stats, /lotofacil, /docs, /health, /ready", status_code=404)

# --------------------------- UI ---------------------------


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    # Página única: UI moderna (dark), sem libs externas
    html = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lotofácil • 12 mais / 3 menos</title>
<style>
:root {
  --bg:#0b1020; --card:#0f172a; --muted:#93a4bd; --fg:#e5eaf1; --soft:#101a33;
  --green:#22c55e; --red:#ef4444; --acc:#38bdf8; --border:#1f2b46;
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0b1020,#0b1020);color:var(--fg);
     font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Arial,sans-serif}
header{padding:18px 16px;border-bottom:1px solid var(--border);
       position:sticky;top:0;background:rgba(11,16,32,.9);backdrop-filter:blur(8px)}
h1{margin:0;font-size:18px;letter-spacing:.2px}
main{max-width:980px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin:12px 0}
.row{display:flex;gap:12px;flex-wrap:wrap}
.control label{font-size:12px;color:var(--muted)}
input,select{background:var(--soft);color:var(--fg);border:1px solid var(--border);
             border-radius:10px;padding:10px 12px;outline:none}
button{background:var(--acc);color:#001a24;border:0;border-radius:10px;
      padding:10px 16px;font-weight:700;cursor:pointer}
button:disabled{opacity:.7;cursor:wait}
small.muted{color:var(--muted)}
.grid15{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-top:10px}
.ball{width:72px;height:72px;border-radius:50%;display:flex;align-items:center;
      justify-content:center;font-weight:800;font-variant-numeric:tabular-nums;box-shadow:0 8px 24px #0004}
.ball.green{background:radial-gradient(ellipse at 30% 30%,#40e07a,#1f9d4d)}
.ball.red{background:radial-gradient(ellipse at 30% 30%,#ff7575,#c53434)}
.barwrap{display:grid;grid-template-columns:repeat(25,1fr);gap:10px;align-items:end;margin-top:14px}
.bar{background:linear-gradient(180deg,#89c4ff,#2a66ff);border-radius:9px 9px 4px 4px;position:relative}
.bar span{position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);
          color:var(--muted);font-size:11px}
.bar label{position:absolute;top:100%;left:50%;transform:translate(-50%,6px);color:var(--muted);font-size:12px}
.table{width:100%;border-collapse:collapse}
.table th,.table td{border-bottom:1px solid var(--border);padding:10px;text-align:left;vertical-align:top}
.badge{display:inline-block;background:#0a1226;border:1px solid var(--border);padding:4px 8px;border-radius:999px;font-size:12px;margin-right:6px}
.footer{text-align:center;color:var(--muted);padding:16px}
@media (max-width:720px){.grid15 .ball{width:60px;height:60px} .barwrap{gap:8px}}
</style>
</head>
<body>
<header>
  <h1>Lotofácil – 12 mais / 3 menos</h1>
</header>
<main>
  <div class="card">
    <div class="row">
      <div class="control"><label>Concursos (limit)</label><br>
        <input id="limit" type="number" min="15" max="120" value="60">
      </div>
      <div class="control"><label>Mais (hi)</label><br>
        <input id="hi" type="number" min="0" max="15" value="12">
      </div>
      <div class="control"><label>Menos (lo)</label><br>
        <input id="lo" type="number" min="0" max="15" value="3">
      </div>
      <div class="control" style="display:flex;align-items:flex-end;gap:10px">
        <label style="display:flex;align-items:center;gap:6px"><input id="force" type="checkbox"> Forçar atualização</label>
        <button id="run">Atualizar</button>
      </div>
    </div>
    <small class="muted" id="meta"></small>
  </div>

  <div class="card" id="comboCard">
    <h3 style="margin:0 0 6px">Combinação sugerida <span class="badge">15 dezenas</span>
      <span class="badge" id="parityBadge"></span></h3>
    <small class="muted" id="subtitle"></small>
    <div class="grid15" id="combo"></div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 6px">Frequência por dezena</h3>
    <div class="barwrap" id="bars"></div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 6px">Últimos concursos</h3>
    <table class="table" id="tbl">
      <thead><tr><th>Concurso</th><th>Data</th><th>Dezenas</th><th>Padrão</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="footer">Fonte: Sorte Online • API pessoal • v__VER__</div>
</main>

<script>
const fmt2 = n => String(n).padStart(2,'0');

async function fetchJSON(u){ const r = await fetch(u); if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }

function urlStats(){
  const p = new URLSearchParams();
  p.set('limit', document.getElementById('limit').value || '60');
  p.set('hi', document.getElementById('hi').value || '12');
  p.set('lo', document.getElementById('lo').value || '3');
  if(document.getElementById('force').checked) p.set('force','true');
  return '/stats?'+p.toString();
}
function urlLotofacil(){
  const p = new URLSearchParams();
  p.set('limit', document.getElementById('limit').value || '60');
  return '/lotofacil?'+p.toString();
}

function renderCombo(s){
  const el = document.getElementById('combo'); el.innerHTML='';
  const hiSet = new Set(s.suggestion.hi); const loSet = new Set(s.suggestion.lo);
  const sorted = s.suggestion.combo;
  sorted.forEach(n=>{
    const d = document.createElement('div');
    d.className = 'ball '+(hiSet.has(n)?'green':'red');
    d.textContent = fmt2(n);
    el.appendChild(d);
  });
  document.getElementById('subtitle').textContent =
    `últimos ${s.limit} concursos • jogos considerados: ${s.considered_games}`;
  const pi = s.suggestion.parity;
  document.getElementById('parityBadge').textContent = `${pi.pattern} (pares/ímpares)`;
}

function renderBars(s){
  const el = document.getElementById('bars'); el.innerHTML='';
  const freqs = s.frequencies.slice().sort((a,b)=>a.n-b.n);
  const maxc = Math.max(...freqs.map(x=>x.count),1);
  freqs.forEach(f=>{
    const b = document.createElement('div');
    b.className = 'bar';
    b.style.height = (Math.round((f.count/maxc)*160)+20)+'px';
    b.innerHTML = `<span>${f.count}</span><label>${fmt2(f.n)}</label>`;
    el.appendChild(b);
  });
}

function renderTable(list){
  const tb = document.querySelector('#tbl tbody'); tb.innerHTML='';
  (list.results||[]).forEach(r=>{
    const tr = document.createElement('tr');
    const nums = (r.numbers||[]).map(fmt2).join(' ');
    tr.innerHTML = `
      <td>${r.contest ?? '-'}</td>
      <td>${r.date ?? '-'}</td>
      <td style="font-family:ui-monospace,monospace">${nums}</td>
      <td>${r.pattern} <span class="badge">${r.even_count}p/${r.odd_count}i</span></td>
    `;
    tb.appendChild(tr);
  });
}

async function load(){
  const btn = document.getElementById('run'); btn.disabled = true;
  try{
    const s = await fetchJSON(urlStats());
    renderCombo(s); renderBars(s);
    const l = await fetchJSON(urlLotofacil());
    renderTable(l);
    document.getElementById('meta').textContent =
      `Método: ${s.method} • Cache: ${s.cache_age_seconds ?? 0}s • Atualizado: ${s.updated_at}`;
  }catch(e){
    alert('Erro: '+(e.message||e));
  }finally{ btn.disabled = false; }
}

document.getElementById('run').addEventListener('click', load);
window.addEventListener('load', load);
</script>
</body></html>
"""
    return HTMLResponse(html.replace("__VER__", APP_VERSION))
