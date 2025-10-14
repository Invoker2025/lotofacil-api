# main.py
# Lotofácil API — FastAPI + Playwright + Parser + Paridade + Retry + Cache curto
# v3.2.0

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None  # fallback se bs4 não estiver instalado

APP_NAME = "Lotofacil API"
APP_VERSION = "3.2.0"
DEFAULT_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

# Cache simples em memória para evitar bater no site a cada request
CACHE_TTL_SECONDS = 120  # 2 min
_cache: Dict[str, Any] = {"ts": 0.0, "html": None}

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via Playwright",
    docs_url="/docs",
    redoc_url=None,
)

# --------------------- Playwright helpers ---------------------


async def render_page(url: str, timeout_ms: int = 45_000) -> str:
    """
    Abre Chromium headless, acessa a URL e retorna o HTML.
    wait_until='domcontentloaded' é mais estável/rápido no Render Free.
    """
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


async def fetch_html(force: bool = False, retries: int = 2, timeout_ms: int = 45_000) -> str:
    # cache curto
    now = time.time()
    if not force and _cache["html"] and (now - _cache["ts"] < CACHE_TTL_SECONDS):
        return _cache["html"]

    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            html = await render_page(DEFAULT_URL, timeout_ms=timeout_ms)
            _cache["html"] = html
            _cache["ts"] = time.time()
            return html
        except Exception as e:
            last_exc = e
            if attempt < retries:
                # pequena espera entre tentativas (ajuda no cold start)
                time.sleep(0.8)
            else:
                raise e
    # só pra tipagem
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

        # acha lista de 15 dezenas
        numbers = None
        for _, v in node.items():
            cand = _normalize_numbers(v)
            if cand:
                numbers = cand
                break
        if not numbers:
            continue

        # concurso
        contest = None
        for k, v in node.items():
            if any(x in k.lower() for x in ("concurso", "contest", "sorteio", "draw")):
                ci = _to_int(v) if isinstance(v, (str, int)) else None
                if ci:
                    contest = ci
                    break

        # data
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
    # bloco de 15 dezenas
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
            snippet_around = html[max(0, idx - 1200): idx + len(b) + 1200]
            m_conc = re_concurso.search(snippet_around)
            m_data = re_data.search(snippet_around)
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

# ------------------------- Rotas --------------------------


@app.get("/", response_class=JSONResponse)
async def root():
    return {
        "message": "Lotofacil API está online!",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
        "examples": {"debug": "/debug?page=1", "lotofacil": "/lotofacil?limit=10"},
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
async def debug(page: Optional[int] = Query(default=1, ge=1, le=10)):
    try:
        html = await fetch_html(force=True, retries=2, timeout_ms=45_000)
        snippet = html[:600].replace("\n", " ")
        return {"page": page, "len": len(html), "snippet": snippet}
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout no debug: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro no debug: {type(e).__name__}: {str(e)}"})


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(
    limit: int = Query(default=10, ge=1, le=50,
                       description="Qtde de concursos para retornar (mais recentes)"),
    # filtros de paridade
    pattern: Optional[str] = Query(
        default=None, pattern=r"^\d{1,2}-\d{1,2}$", description="Ex.: 8-7 (pares-ímpares)"),
    min_even: Optional[int] = Query(default=None, ge=0, le=15),
    max_even: Optional[int] = Query(default=None, ge=0, le=15),
    # cache
    force: bool = Query(
        default=False, description="Se true, ignora cache curto e força novo scrape"),
):
    """
    Renderiza a página, extrai concursos e calcula pares/ímpares.
    Filtros opcionais: pattern=8-7, min_even/max_even. Cache curto (~2min).
    """
    try:
        html = await fetch_html(force=force, retries=2, timeout_ms=60_000)
        parsed = parse_lotofacil(html, limit=limit)

        # pós-processa paridade
        items = []
        for r in parsed["results"]:
            p = parity_info(r["numbers"])
            items.append({**r, **p})

        # filtros de paridade
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

        # resumo
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
            "limit": limit,
            "filters": {"pattern": pattern, "min_even": min_even, "max_even": max_even, "force": force},
            "summary": summary,
            "count": len(items),
            "results": items,
            "source_url": DEFAULT_URL,
            "method": parsed["method"],
            "cache_age_seconds": round(time.time() - _cache["ts"], 2) if _cache["html"] else None,
        }

    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout na lotofacil: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"})


@app.exception_handler(404)
async def not_found(_, __):
    return PlainTextResponse("Not Found. Veja /, /docs, /health, /ready, /debug ou /lotofacil", status_code=404)
