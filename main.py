# main.py
# Lotofácil API — renderizando SorteOnline com Playwright (opção 2)
# versão 2.1.0

import os
import time
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from playwright.async_api import (
    async_playwright,
    TimeoutError as PWTimeoutError,
)

APP_NAME = "Lotofácil API (SorteOnline)"
APP_VERSION = "2.1.0"

SORTEONLINE_BASE = "https://www.sorteonline.com.br/lotofacil/resultados"

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    summary="Resultados da Lotofácil via renderização Playwright",
    docs_url="/docs",
    redoc_url=None,
)


# ======================================================================
# Utilitário: checar se Chromium existe (apenas para /health exibir)
# ======================================================================
def _has_chromium_cache() -> bool:
    # Render instala o Chromium Headless Shell sob esta pasta
    cache_root = "/opt/render/.cache/ms-playwright"
    try:
        return os.path.exists(cache_root) and any(
            name.startswith("chromium") for name in os.listdir(cache_root)
        )
    except Exception:
        return False


# ======================================================================
# Espera de DOM estável (evita erro "Page is navigating/changing content")
# ======================================================================
async def wait_for_stable_dom(
    page,
    min_stable_ms: int = 1200,
    window_ms: int = 300,
    max_wait_ms: int = 15000,
) -> bool:
    """
    Considera o DOM estável quando o tamanho do HTML não muda por
    'min_stable_ms'. Checa a cada 'window_ms' até 'max_wait_ms'.
    """
    start = time.monotonic()
    last_len = -1
    stable_for = 0
    while (time.monotonic() - start) * 1000 < max_wait_ms:
        html = await page.evaluate("document.documentElement.outerHTML")
        cur_len = len(html)
        if cur_len == last_len:
            stable_for += window_ms
            if stable_for >= min_stable_ms:
                return True
        else:
            stable_for = 0
        last_len = cur_len
        await page.wait_for_timeout(window_ms)
    return False


# ======================================================================
# Renderizador (abre página com headers/locale realistas e espera robusta)
# ======================================================================
async def render_html(app: FastAPI, page_num: int = 1) -> str:
    url = f"{SORTEONLINE_BASE}?pagina={page_num}"

    browser = app.state.browser
    ctx = await browser.new_context(
        locale="pt-BR",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.sorteonline.com.br/",
        },
    )
    page = await ctx.new_page()
    try:
        # carrega DOM mínimo
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # tenta estado de rede ociosa; se não der, seguimos mesmo assim
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeoutError:
            pass

        # espera DOM parar de mudar
        await wait_for_stable_dom(
            page,
            min_stable_ms=1200,
            window_ms=300,
            max_wait_ms=20_000,
        )

        html = await page.evaluate("document.documentElement.outerHTML")
        return html
    finally:
        await ctx.close()


# ======================================================================
# Lifecycle — inicia/encerra o Playwright (um browser global)
# ======================================================================
@app.on_event("startup")
async def startup():
    pw = await async_playwright().start()
    # flags recomendadas para ambientes serverless/containers
    launch_args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-dev-tools",
    ]
    browser = await pw.chromium.launch(
        headless=True,
        args=launch_args,
    )
    app.state._pw = pw
    app.state.browser = browser


@app.on_event("shutdown")
async def shutdown():
    try:
        await app.state.browser.close()
    except Exception:
        pass
    try:
        await app.state._pw.stop()
    except Exception:
        pass


# ======================================================================
# Endpoints
# ======================================================================
@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health, /debug?page=1 ou /lotofacil?months=3",
        "docs": "/docs",
        "version": APP_VERSION,
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "message": "Lotofácil API online!",
        "chromium": _has_chromium_cache(),
        "version": APP_VERSION,
    }


@app.get("/debug")
async def debug(page: int = Query(1, ge=1, description="Número da página em SorteOnline")):
    try:
        html = await render_html(app, page)
        return {
            "page": page,
            "len": len(html),
            "snippet": html[:1000],  # facilita inspeção rápida
        }
    except PWTimeoutError as e:
        return JSONResponse(
            status_code=504,
            content={
                "detail": f"Timeout ao renderizar a página (Playwright): {str(e)}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Erro no debug (render): {type(e).__name__}: {str(e)}"},
        )


@app.get("/lotofacil")
async def lotofacil(months: int = Query(3, ge=1, le=24)):
    """
    Por enquanto, apenas valida a renderização e retorna o tamanho do HTML.
    Em seguida, plugaremos o parser para retornar:
      { "meses": X, "qtd": N, "concursos": [...] }
    """
    try:
        html = await render_html(app, page_num=1)
        # TODO: implementar parser dos concursos a partir do HTML do SorteOnline.
        return {"meses": months, "qtd": 0, "raw_len": len(html)}
    except PWTimeoutError as e:
        return JSONResponse(
            status_code=504,
            content={"detail": f"Timeout na lotofacil (render): {str(e)}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Erro na lotofacil: {type(e).__name__}: {str(e)}"},
        )
