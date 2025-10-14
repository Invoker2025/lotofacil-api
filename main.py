# main.py
# Lotofácil API — FastAPI + Playwright + Parser de resultados
# v3.0.0

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None  # fallback se bs4 não estiver instalado

APP_NAME = "Lotofacil API"
APP_VERSION = "3.0.0"

DEFAULT_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via Playwright",
    docs_url="/docs",
    redoc_url=None,
)

# --------------------- Playwright helper ---------------------


async def render_page(url: str, timeout_ms: int = 45_000) -> str:
    """
    Abre Chromium headless, acessa a URL e retorna o HTML.
    Usa wait_until="domcontentloaded" (mais rápido/estável no Render Free).
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

# --------------------- Parser helpers -----------------------


def _to_int(x: Any) -> Optional[int]:
    try:
        s = str(x).strip()
        if not s:
            return None
        return int(s)
    except Exception:
        return None


def _looks_number_list(v: Any) -> bool:
    """
    Heurística: lista com >=15 valores (1..25) — típico de 15 dezenas da Lotofácil
    """
    if not isinstance(v, (list, tuple)):
        return False
    nums = []
    for item in v:
        n = _to_int(item)
        if n is None:
            return False
        nums.append(n)
    return len(nums) >= 15 and all(1 <= n <= 25 for n in nums)


def _pick_first(d: Dict[str, Any], keys_like: Iterable[str]) -> Optional[Any]:
    for k, v in d.items():
        lk = k.lower()
        if any(needle in lk for needle in keys_like):
            return v
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
    """
    Iterador DFS sobre qualquer estrutura (dict/list/tuple/…)
    """
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)


def parse_from_next_data(html: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Tenta extrair os concursos do JSON Next.js (__NEXT_DATA__).
    Retorna lista de dicts com: contest, date, numbers (15), [city/state] quando disponível.
    """
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

        # Pistas de que o node é da Lotofácil
        texto = json.dumps(node, ensure_ascii=False).lower()
        if "loto" not in texto and "facil" not in texto and "lotofácil" not in texto:
            # ainda assim pode conter o bloco correto; não filtramos agressivo
            pass

        # Procura uma lista de 15 números
        numbers = None
        for k, v in node.items():
            cand = _normalize_numbers(v)
            if cand:
                numbers = cand
                break

        if not numbers:
            continue

        # Concurso
        contest = None
        for k, v in node.items():
            lk = k.lower()
            if "concurso" in lk or "contest" in lk or "sorteio" in lk or "draw" in lk:
                if isinstance(v, (str, int)):
                    ci = _to_int(v)
                    if ci:
                        contest = ci
                        break

        # Data (string)
        date = None
        for k, v in node.items():
            lk = k.lower()
            if "data" in lk or "date" in lk:
                if isinstance(v, str) and len(v) >= 8:
                    date = v
                    break

        # Cidade/UF (se existir)
        city = None
        uf = None
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

        if len(results) >= max(1, limit):
            break

    # Ordena por número do concurso (desc) quando possível
    results.sort(key=lambda x: (
        x["contest"] is not None, x["contest"]), reverse=True)
    return results[:limit]


def parse_from_html_regex(html: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Fallback: usa regex para localizar blocos de 15 dezenas e (se possível) o número/data do concurso.
    Menos confiável, mas suficiente quando o JSON não está disponível.
    """
    results: List[Dict[str, Any]] = []

    # Encontra todas as sequências com 15 dezenas (1..25), em ordem.
    # Aceita "01" ou "1", separados por espaço, vírgula ou <li>/<span> etc.
    dezena = r"(?:0?[1-9]|1\d|2[0-5])"
    sep = r"(?:\s*[,|/•\-]?\s*|</[^>]+>\s*<[^>]+>)"  # separadores flexíveis
    bloco = rf"({dezena}(?:{sep}{dezena}){{14}})"
    blocks = re.findall(bloco, html)

    # Concurso (pegamos o mais próximo, depois ajustamos)
    # Exemplos no HTML: "Concurso 3511", "concurso n° 3511", "Result. da Lotofácil 3511"
    re_concurso = re.compile(
        r"concurs(?:o|o nº|o n[oº])\s*[:#]?\s*(\d{3,5})", re.I)
    # Data (bem flex): 12/10/2025, 2025-10-12, 12 de out, etc.
    re_data = re.compile(
        r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\d{4}[\/\-]\d{1,2}[\/\-]\d{1,2}|"
        r"\d{1,2}\s+de\s+[A-Za-zçãéíóú]+\s+\d{2,4})",
        re.I,
    )

    for b in blocks:
        # Normaliza e transforma em lista de ints
        nums = re.findall(r"\d{1,2}", b)
        cand = [_to_int(n) for n in nums]
        cand = [n for n in cand if n is not None]
        if len(cand) >= 15:
            cand = cand[:15]
            if all(1 <= n <= 25 for n in cand):
                # tenta achar concurso e data próximos do bloco
                # (janela de 1200 chars ao redor)
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

    # Dedup por (contest, tuple(numbers))
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
    """
    Pipeline de parsing: tenta Next.js JSON; se falhar, usa regex.
    """
    data = parse_from_next_data(html, limit=limit)
    method = "next_data"
    if not data:
        data = parse_from_html_regex(html, limit=limit)
        method = "regex_html"

    return {
        "count": len(data),
        "method": method,
        "results": data,
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
        "examples": {"debug": "/debug?page=1", "lotofacil": "/lotofacil?months=3"},
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
        html = await render_page(DEFAULT_URL, timeout_ms=45_000)
        snippet = html[:600].replace("\n", " ")
        return {"page": page, "len": len(html), "snippet": snippet}
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout no debug: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro no debug: {type(e).__name__}: {str(e)}"})


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(
    months: int = Query(default=3, ge=1, le=24,
                        description="Compatibilidade — não usado como filtro real"),
    limit: int = Query(default=10, ge=1, le=50,
                       description="Qtde de concursos para retornar (mais recentes)"),
):
    """
    Renderiza a página de resultados e extrai os concursos.
    - `limit` controla quantos concursos retornam (default 10).
    - `months` mantido apenas por compatibilidade.
    """
    try:
        html = await render_page(DEFAULT_URL, timeout_ms=60_000)
        parsed = parse_lotofacil(html, limit=limit)
        return {
            "ok": True,
            "limit": limit,
            "months": months,
            **parsed,
            "source_url": DEFAULT_URL,
        }
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout na lotofacil: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"})


@app.exception_handler(404)
async def not_found(_, __):
    return PlainTextResponse("Not Found. Veja /, /docs, /health, /ready, /debug ou /lotofacil", status_code=404)
