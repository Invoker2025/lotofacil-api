# main.py
# Lotofácil API — renderizando páginas com Playwright para uso pessoal
# Compatível com Render Free (lazy-start do Chromium)
# v2.2.0

import asyncio
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

APP_NAME = "Lotofacil API"
APP_VERSION = "2.2.0"

# URL padrão de exemplo (troque pela que você realmente usa)
DEFAULT_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

# ---------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------
app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via renderização (Playwright)",
    docs_url="/docs",
    redoc_url=None,
)


# ---------------------------------------------------------------------
# Util: renderizar uma página com Playwright (abre/fecha no próprio request)
# Evita manter browser vivo (bom para Render Free que hiberna)
# ---------------------------------------------------------------------
async def render_html(url: str, wait_selector: Optional[str] = None, timeout_ms: int = 25_000) -> str:
    """
    Abre Chromium em modo headless, acessa a URL e retorna o HTML.
    - wait_selector: CSS opcional para aguardar um elemento específico.
    - timeout_ms: timeout total por request (ms).
    """
    # Alguns flags ajudam em ambientes de container (Render/Heroku, etc.)
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
            await page.goto(url, timeout=timeout_ms)

            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=timeout_ms)

            html = await page.content()
            return html
        finally:
            await browser.close()


# ---------------------------------------------------------------------
# Raiz (evita "Not Found" ao acessar o domínio)
# ---------------------------------------------------------------------
@app.get("/", response_class=JSONResponse)
async def root():
    return {
        "message": "Lotofacil API está online!",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "examples": {
            "debug": "/debug?page=1",
            "lotofacil": "/lotofacil?months=3",
        },
    }


# ---------------------------------------------------------------------
# Healthcheck (abre e fecha o Chromium rapidamente)
# ---------------------------------------------------------------------
@app.get("/health", response_class=JSONResponse)
async def health_check():
    try:
        # carrega uma página simples, rápida e confiável
        html = await render_html("https://example.com", timeout_ms=10_000)
        ok = bool(html and "<html" in html.lower())
        return {"status": "ok" if ok else "error", "chromium": ok}
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": f"{type(e).__name__}: {e}"},
        )


# ---------------------------------------------------------------------
# Debug (exemplo simples: baixa a 1ª página com Playwright e retorna len/snippet)
# ---------------------------------------------------------------------
@app.get("/debug", response_class=JSONResponse)
async def debug(page: Optional[int] = Query(default=1, ge=1, le=10)):
    try:
        html = await render_html(DEFAULT_URL, timeout_ms=25_000)
        snippet = html[:400].replace("\n", " ")
        return {"page": page, "len": len(html), "snippet": snippet}
    except PWTimeoutError as e:
        return JSONResponse(
            status_code=504,
            content={"detail": f"Timeout no debug: {str(e)}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"detail": f"Erro no debug: {type(e).__name__}: {str(e)}"},
        )


# ---------------------------------------------------------------------
# Endpoint principal (esqueleto): /lotofacil?months=3
# Aqui você pode implementar o parser específico da página
# ---------------------------------------------------------------------
@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(months: int = Query(default=3, ge=1, le=24)):
    """
    Esqueleto do endpoint principal.
    - Faz render da página de resultados e devolve métricas básicas.
    - Onde indicado, você pode implementar o parser dos concursos.
    """
    try:
        html = await render_html(DEFAULT_URL, timeout_ms=30_000)

        # TODO: Implementar aqui seu parser dos concursos/resultados.
        # Ex.: extrair data/concurso/dezenas com BeautifulSoup ou page.locator().
        # Por ora retornamos somente métricas básicas + snippet.
        snippet = html[:600].replace("\n", " ")
        return {
            "months": months,
            "html_len": len(html),
            "snippet": snippet,
            "note": "Parser ainda não implementado neste esqueleto.",
        }

    except PWTimeoutError as e:
        return JSONResponse(
            status_code=504,
            content={"detail": f"Timeout na lotofacil: {str(e)}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"},
        )


# ---------------------------------------------------------------------
# Rota de fallback para 404 mais amigável (opcional)
# ---------------------------------------------------------------------
@app.exception_handler(404)
async def not_found(_, __):
    return PlainTextResponse(
        "Not Found. Veja /, /docs, /health, /debug ou /lotofacil",
        status_code=404,
    )
