# main.py
# Lotofácil API – SorteOnline + Playwright
# ----------------------------------------
# Observação importante:
# 1) Este arquivo garante a instalação do Chromium no startup do Render.
# 2) Endpoints: / (root), /health, /debug, /lotofacil?months=N
# 3) Scraping no SorteOnline com Playwright (headless), respeitando robots/limites.

from playwright.async_api import async_playwright
import asyncio
import os
import re
import sys
import json
import time
import math
import traceback
import subprocess
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
from pydantic import BaseModel

# -------------------------------------------------------------
# 1) Garante que o Chromium do Playwright seja instalado
#    (essencial para o Render Free/Starter)
# -------------------------------------------------------------


def ensure_playwright_browsers() -> None:
    """
    Baixa o Chromium do Playwright caso ainda não esteja instalado.
    Executa rápido se já existir em cache do Render.
    """
    try:
        # Primeiro, tentamos instalar apenas o browser Chromium
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception as e:
        # Em ambientes que precisam de libs do SO, podemos tentar install-deps (ignora erro)
        try:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as e2:
            print(
                f"[WARN] Falha ao instalar Chromium do Playwright: {e2}\n{traceback.format_exc()}")


# chama a garantia de browsers ANTES de criar a app
ensure_playwright_browsers()

# -------------------------------------------------------------
# 2) App & modelos
# -------------------------------------------------------------
APP_NAME = "Lotofácil API (SorteOnline)"
VERSION = "2.2.0"

app = FastAPI(title=APP_NAME, version=VERSION)

# Estado compartilhado (browser/page)


class AppState(BaseModel):
    started_at: float = time.time()
    chromium_ok: bool = False


app.state.meta = AppState()
app.state.browser = None
app.state.context = None
app.state.page = None

# -------------------------------------------------------------
# 3) Utilidades
# -------------------------------------------------------------
SORT_ONLINE_URL = "https://www.sorteonline.com.br/lotofacil/resultados?pagina={page}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

HEADLESS = True  # Render não permite sandbox interativo
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-dev-tools",
    "--no-zygote",
    "--single-process",
]

# Regex útil para encontrar número do concurso / data
RE_CONCURSO = re.compile(r"concurso\s*(\d+)", re.IGNORECASE)
RE_DATA = re.compile(r"(\d{2}/\d{2}/\d{4})")


def parse_int(s: str) -> Optional[int]:
    try:
        return int(re.sub(r"[^\d]", "", s))
    except Exception:
        return None


def parse_bolas(soup: BeautifulSoup) -> List[int]:
    bolas = []
    # Números geralmente aparecem como badges/bolinhas
    for badge in soup.select("ul li, .badge, .dezena, .ball, .resultado-numero"):
        txt = badge.get_text(strip=True)
        if txt and txt.isdigit():
            n = parse_int(txt)
            if n is not None and 1 <= n <= 25:
                bolas.append(n)
    # evita ruído, Lotofácil sempre 15 dezenas
    if len(bolas) >= 15:
        return bolas[:15]
    return bolas


def parse_premios(soup: BeautifulSoup) -> Dict[str, Any]:
    # Tenta capturar algum resumo de prêmios quando disponível
    data = {}
    tab = soup.find("table")
    if not tab:
        return data
    headers = [th.get_text(" ", strip=True) for th in tab.select("thead th")]
    for tr in tab.select("tbody tr"):
        cols = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cols) == len(headers):
            row = dict(zip(headers, cols))
            # heurística:
            if "Acertos" in row:
                data[row["Acertos"]] = row
    return data


def parse_concursos_from_html(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    concursos: List[Dict[str, Any]] = []

    # Cada cartão de resultado costuma ficar em seções/divs específicas
    # Variamos os seletores para tolerar mudanças leves de layout
    cards = soup.select(
        "section, article, div.card, div.resultado, div[class*='resultado']")
    if not cards:
        # fallback: tenta por listas grandes
        cards = soup.select("div")

    for block in cards:
        txt = block.get_text(" ", strip=True)
        if not txt:
            continue

        # detecta presença de "Concurso NNNN" no bloco
        m = RE_CONCURSO.search(txt)
        if not m:
            continue
        concurso = parse_int(m.group(1))
        if not concurso:
            continue

        m2 = RE_DATA.search(txt)
        data_str = m2.group(1) if m2 else None

        bolas = parse_bolas(block)
        if not bolas:
            # pode estar em sub-árvore específica
            inner = block.select_one(".dezenas, ul.dezenas, .numeros")
            if inner:
                bolas = parse_bolas(inner)

        premios = parse_premios(block)

        concursos.append({
            "concurso": concurso,
            "data": data_str,
            "dezenas": bolas,
            "premios": premios,
        })

    # remove duplicatas por concurso (mantém o primeiro)
    seen = set()
    unique = []
    for c in concursos:
        k = c["concurso"]
        if k in seen:
            continue
        seen.add(k)
        unique.append(c)

    return unique


async def goto_and_html(page, url: str, wait_selector: Optional[str] = None, timeout_ms: int = 25000) -> str:
    await page.set_extra_http_headers({"Referer": "https://www.sorteonline.com.br/"})
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    # Tenta esperar por algo típico de resultados
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=timeout_ms)
        except Exception:
            pass
    # Pequeno delay para JS terminar de compor a árvore
    await asyncio.sleep(0.6)
    return await page.content()

# -------------------------------------------------------------
# 4) Lifecycle: abre e fecha o Playwright/Chromium
# -------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    """
    Sobe Chromium headless e mantém uma única page viva para as requisições.
    """
    try:
        pw = await async_playwright().start()
        app.state._pw = pw
        browser = await pw.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
        ctx = await browser.new_context(user_agent=UA, locale="pt-BR", timezone_id="America/Sao_Paulo")
        page = await ctx.new_page()
        # (Opcional) set viewport fixo
        await page.set_viewport_size({"width": 1280, "height": 800})

        app.state.browser = browser
        app.state.context = ctx
        app.state.page = page
        app.state.meta.chromium_ok = True
        print("[startup] Chromium ok.")
    except Exception as e:
        app.state.meta.chromium_ok = False
        print(
            f"[startup] Falha ao iniciar Chromium: {e}\n{traceback.format_exc()}")


@app.on_event("shutdown")
async def shutdown() -> None:
    try:
        if app.state.page:
            await app.state.page.close()
        if app.state.context:
            await app.state.context.close()
        if app.state.browser:
            await app.state.browser.close()
        if getattr(app.state, "_pw", None):
            await app.state._pw.stop()
        print("[shutdown] Playwright fechado.")
    except Exception as e:
        print(f"[shutdown] Erro: {e}")

# -------------------------------------------------------------
# 5) Endpoints
# -------------------------------------------------------------


@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health, /debug?page=1 ou /lotofacil?months=3",
        "docs": "/docs",
        "version": VERSION,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "message": "Lotofácil API online!", "chromium": app.state.meta.chromium_ok}


@app.get("/debug")
async def debug(page: int = Query(1, ge=1)):
    """
    Faz o fetch de uma página e devolve informações de diagnóstico:
    - status 'ok' (se Chromium subiu)
    - url alvo
    - trecho do HTML
    """
    if not app.state.meta.chromium_ok or not app.state.page:
        raise HTTPException(
            status_code=500, detail="Chromium não está pronto (startup falhou).")

    url = SORT_ONLINE_URL.format(page=page)
    try:
        html = await goto_and_html(app.state.page, url, wait_selector="main, body")
        snippet = re.sub(r"\s+", " ", html[:1200])  # trechinho
        return {
            "page": page,
            "url": url,
            "len_html": len(html),
            "snippet": snippet,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro no debug: {e}")


@app.get("/lotofacil")
async def lotofacil(months: int = Query(3, ge=1, le=24)):
    """
    Coleta resultados da Lotofácil dos últimos N meses no SorteOnline.
    Percorre páginas (1, 2, 3, ...) até cobrir o período.
    """
    if not app.state.meta.chromium_ok or not app.state.page:
        raise HTTPException(
            status_code=500, detail="Chromium não está pronto (startup falhou).")

    # Período alvo:
    hoje = datetime.now()
    limite = hoje - timedelta(days=30 * months)

    concursos: List[Dict[str, Any]] = []
    page_idx = 1
    MAX_PAGES = 30  # limite de segurança
    try:
        while page_idx <= MAX_PAGES:
            url = SORT_ONLINE_URL.format(page=page_idx)
            html = await goto_and_html(app.state.page, url, wait_selector="main, body")
            page_concursos = parse_concursos_from_html(html)

            if not page_concursos:
                # nada encontrado -> para
                break

            # filtrar por data (se disponível), e ir acumulando
            stop = False
            for c in page_concursos:
                d = None
                if c.get("data"):
                    try:
                        d = datetime.strptime(c["data"], "%d/%m/%Y")
                    except Exception:
                        d = None
                if d and d < limite:
                    stop = True
                    continue
                concursos.append(c)

            if stop:
                break

            page_idx += 1
            # pequeno respiro entre páginas
            await asyncio.sleep(0.4)

        # ordena por concurso desc
        concursos.sort(key=lambda x: x.get("concurso", 0), reverse=True)

        return {"meses": months, "qtd": len(concursos), "concursos": concursos}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Erro ao coletar Lotofácil: {e}")

# -------------------------------------------------------------
# 6) Execução local
# -------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
