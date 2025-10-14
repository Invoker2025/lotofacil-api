# main.py
# Lotofácil API — FastAPI + Playwright (Render Free friendly)
# v2.3.0

import asyncio
from typing import Optional

from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

APP_NAME = "Lotofacil API"
APP_VERSION = "2.3.0"
DEFAULT_URL = "https://www.sorteonline.com.br/lotofacil/resultados"

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via renderização (Playwright)",
    docs_url="/docs",
    redoc_url=None,
)

# ------------------------- Utils -------------------------


async def render_html(url: str, wait_selector: Optional[str] = None, timeout_ms: int = 25_000) -> str:
    """Abre Chromium em headless, acessa a URL e retorna o HTML."""
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

# ------------------------- Raiz --------------------------


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
            "lotofacil": "/lotofacil?months=3",
        },
    }

# Alguns provedores fazem HEAD / antes do health check


@app.head("/")
async def root_head():
    return Response(status_code=200)

# ------------------------ Health -------------------------
# Health MUITO rápido: não abre Chromium (evita timeouts do Render)


@app.get("/health", response_class=JSONResponse)
async def health_check():
    return {"status": "ok", "app": APP_NAME, "version": APP_VERSION}

# O Render costuma usar HEAD no health; garantimos 200


@app.head("/health")
async def health_head():
    return Response(status_code=200)

# ------------------------ Ready --------------------------
# "Pronto" de verdade (abre e fecha o Chromium). Use quando quiser testar o browser.


@app.get("/ready", response_class=JSONResponse)
async def ready_check():
    try:
        _ = await render_html("https://example.com", timeout_ms=10_000)
        return {"status": "ok", "chromium": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "detail": f"{type(e).__name__}: {e}"})

# ------------------------- Debug -------------------------


@app.get("/debug", response_class=JSONResponse)
async def debug(page: Optional[int] = Query(default=1, ge=1, le=10)):
    try:
        html = await render_html(DEFAULT_URL, timeout_ms=25_000)
        snippet = html[:400].replace("\n", " ")
        return {"page": page, "len": len(html), "snippet": snippet}
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout no debug: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro no debug: {type(e).__name__}: {str(e)}"})

# ---------------------- Endpoint principal ----------------------


@app.get("/lotofacil", response_class=JSONResponse)
async def lotofacil(months: int = Query(default=3, ge=1, le=24)):
    """
    Esqueleto do endpoint principal.
    """
    try:
        html = await render_html(DEFAULT_URL, timeout_ms=30_000)
        # TODO: implementar parser de resultados aqui
        snippet = html[:600].replace("\n", " ")
        return {
            "months": months,
            "html_len": len(html),
            "snippet": snippet,
            "note": "Parser ainda não implementado neste esqueleto.",
        }
    except PWTimeoutError as e:
        return JSONResponse(status_code=504, content={"detail": f"Timeout na lotofacil: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=502, content={"detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"})

# --------------------- 404 amigável ----------------------


@app.exception_handler(404)
async def not_found(_, __):
    return PlainTextResponse("Not Found. Veja /, /docs, /health, /ready, /debug ou /lotofacil", status_code=404)
