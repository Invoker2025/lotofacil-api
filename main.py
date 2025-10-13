import os
import re
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
from pydantic import BaseModel

# Playwright (assíncrono)
from playwright.async_api import async_playwright, Browser, Page

APP_NAME = "Lotofácil API (Playwright)"
BASE_URL = "https://www.sorteonline.com.br/lotofacil/resultados?pagina={page}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

app = FastAPI(title=APP_NAME, version="2.1.0")

browser: Optional[Browser] = None  # será iniciado no startup


class Concurso(BaseModel):
    concurso: int
    data: str      # ISO: YYYY-MM-DD
    dezenas: List[int]


def parse_date_pt(text: str) -> Optional[datetime]:
    """
    Aceita datas no formato 'dd/mm/aaaa' que aparecem no SorteOnline.
    """
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    try:
        return datetime(y, mth, d)
    except ValueError:
        return None


def extract_concursos_from_html(html: str) -> List[Concurso]:
    """
    Extrai múltiplos concursos de um HTML renderizado do SorteOnline.
    Estratégia 'defensiva': usa BeautifulSoup + regex para tolerar mudanças de CSS.
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) Tenta achar "cards" por palavras típicas
    cards: List[BeautifulSoup] = []
    for tag in soup.find_all(True):
        txt = tag.get_text(" ", strip=True)
        if "Concurso" in txt and re.search(r"\b\d{2}/\d{2}/\d{4}\b", txt):
            # Evita tags super pequenas
            if len(txt) > 40:
                cards.append(tag)

    # Dedup "cards" por hash do texto
    seen = set()
    uniq_cards = []
    for t in cards:
        key = t.get_text(" ", strip=True)
        if key not in seen:
            seen.add(key)
            uniq_cards.append(t)

    resultados: List[Concurso] = []

    for block in uniq_cards:
        text = block.get_text(" ", strip=True)

        # Concurso #
        mc = re.search(r"Concurso\s*(\d+)", text, flags=re.I)
        if not mc:
            continue
        num = int(mc.group(1))

        # Data
        dt = parse_date_pt(text)
        if not dt:
            continue

        # 15 dezenas (dois dígitos) em sequência no bloco
        # Aceita separadores variados (espaços, vírgulas, quebras)
        dezenas = re.findall(r"\b(\d{2})\b", text)
        # Em alguns trechos o bloco é muito verboso; vamos tentar focar nas "bolinhas"
        # procurando classes comuns de números (fallback abaixo)
        if len(dezenas) < 15:
            nums = []
            for elm in block.find_all(True):
                cls = " ".join(elm.get("class", []))
                if re.search(r"(bola|dezena|number|num|lottery|resultado)", cls, re.I):
                    t = elm.get_text(strip=True)
                    if re.fullmatch(r"\d{2}", t):
                        nums.append(t)
            if len(nums) >= 15:
                dezenas = nums

        # filtra estritamente 15 dezenas, converte para int e ordena crescente
        only_two_digits = [int(d)
                           for d in dezenas if re.fullmatch(r"\d{2}", d)]
        if len(only_two_digits) < 15:
            # último recurso: pega as primeiras 15 válidas do bloco
            only_two_digits = only_two_digits[:15]
        if len(only_two_digits) != 15:
            # não conseguimos 15 dezenas -> ignora o bloco
            continue

        only_two_digits.sort()
        resultados.append(
            Concurso(concurso=num, data=dt.date().isoformat(),
                     dezenas=only_two_digits)
        )

    # Remove duplicados por número do concurso (mantém o mais novo)
    by_concurso: Dict[int, Concurso] = {}
    for c in sorted(resultados, key=lambda x: x.concurso, reverse=True):
        by_concurso.setdefault(c.concurso, c)

    return list(sorted(by_concurso.values(), key=lambda x: x.concurso, reverse=True))


async def get_html(page: Page, url: str) -> str:
    """
    Carrega a URL com Playwright e devolve o HTML renderizado.
    """
    await page.route("**/*", lambda route: route.continue_())
    await page.goto(url, wait_until="networkidle", timeout=45_000)
    # Alguns sites só exibem números após um pequeno tempo
    await page.wait_for_timeout(1200)
    return await page.content()


@app.on_event("startup")
async def startup() -> None:
    """
    Sobe o Chromium headless no boot do serviço.
    """
    global browser
    if browser:
        return
    pw = await async_playwright().start()
    # Render Free costuma exigir --no-sandbox
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    global browser
    try:
        if browser:
            await browser.close()
    except Exception:
        pass
    browser = None


@app.get("/", tags=["Root"])
def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health, /debug?page=1 ou /lotofacil?months=3",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug", tags=["Debug"])
async def debug(page: int = Query(1, ge=1)):
    """
    Retorna status e um snippet do HTML da página pedida (já renderizada).
    """
    if not browser:
        raise HTTPException(503, "Browser não inicializado.")
    context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 2000})
    page_obj = await context.new_page()

    url = BASE_URL.format(page=page)
    try:
        html = await get_html(page_obj, url)
        soup = BeautifulSoup(html, "lxml")
        snippet = soup.get_text(" ", strip=True)[:900]
        return {"url": url, "status_code": 200, "qtd_text": len(snippet), "snippet": snippet}
    finally:
        await context.close()


@app.get("/lotofacil", tags=["Lotofacil"])
async def lotofacil(months: int = Query(3, ge=1, le=24)):
    """
    Agrega concursos dos últimos 'months' meses a partir do SorteOnline,
    paginando até atingir o limite temporal.
    """
    if not browser:
        raise HTTPException(503, "Browser não inicializado.")

    limite = datetime.now().date() - timedelta(days=30 * months)
    coletados: List[Concurso] = []
    page_num = 1
    MAX_PAGES = 30  # segurança para não varrer infinito

    context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 2400})
    page_obj = await context.new_page()

    try:
        while page_num <= MAX_PAGES:
            url = BASE_URL.format(page=page_num)
            html = await get_html(page_obj, url)
            concursos = extract_concursos_from_html(html)

            if not concursos:
                # página sem resultados parseáveis -> para
                break

            # Filtra por data
            antigos = 0
            for c in concursos:
                d = datetime.fromisoformat(c.data).date()
                if d >= limite:
                    coletados.append(c)
                else:
                    antigos += 1

            # Se a maioria já é antiga, paramos
            if antigos >= len(concursos) // 2:
                break

            page_num += 1

        # Dedup final por número
        seen = set()
        final: List[Dict[str, Any]] = []
        for c in sorted(coletados, key=lambda x: x.concurso, reverse=True):
            if c.concurso in seen:
                continue
            seen.add(c.concurso)
            final.append(c.dict())

        return JSONResponse(
            {
                "meses": months,
                "qtd": len(final),
                # mantive a chave como concuros (sem acento) para estabilidade
                "concuros": final,
            }
        )

    except Exception as e:
        raise HTTPException(
            500, f"Falha ao coletar SorteOnline via Playwright: {e}") from e
    finally:
        await context.close()
