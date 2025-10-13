import os
import re
import traceback
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

APP_NAME = "Lotofácil API (AS Loterias)"
SCRAPER_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()

app = FastAPI(title=APP_NAME, version="2.1.0")


# --------- util: chamada via ScraperAPI ----------
def fetch_via_scraper(url: str, render: bool = True, raise_for_status: bool = False):
    """
    Busca a URL informada passando pelo ScraperAPI.
    - render=True: usa navegador headless (para páginas com JS)
    - raise_for_status: se True, lança requests.HTTPError em 4xx/5xx
    Retorna (status_code, text, headers_used)
    """
    if not SCRAPER_KEY:
        return 500, "", {"error": "SCRAPERAPI_KEY ausente nas variáveis de ambiente"}

    api = "https://api.scraperapi.com"
    params = {
        "api_key": SCRAPER_KEY,
        "url": url,
        "country_code": "br",
        "keep_headers": "true",
    }
    if render:
        params["render"] = "true"

    # cabeçalhos "normais" de um navegador
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.asloterias.com.br/",
        "Connection": "keep-alive",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    res = requests.get(api, params=params, headers=headers, timeout=60)
    if raise_for_status:
        res.raise_for_status()
    return res.status_code, res.text, {"headers_used": headers}


# --------- parser p/ AS Loterias (versão defensiva) ----------
def parse_asloterias_list(html: str) -> List[Dict[str, Any]]:
    """
    Tenta extrair concursos da página de resultados da Lotofácil em AS Loterias.
    Esta função é defensiva: tenta múltiplas estratégias.
    Retorna lista de objetos: { concurso, data, dezenas: [..] }
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) procurar blocos que tenham 15 dezenas (ex.: “li”/“span” com 2 dígitos)
    concursos: List[Dict[str, Any]] = []

    # estratégia genérica: procurar grupos de 15 números (1–25)
    # isso é só para termos algo funcionando; você pode melhorar conforme o HTML real
    texto = soup.get_text(" ", strip=True)
    # encontra sequências de números com 1–2 dígitos (1..25)
    candidatos = re.findall(r"(?:\b(?:[01]?\d|2[0-5])\b[\s,;:-]*){10,}", texto)
    # Aumentei o limiar para >10 para filtrar lixos; depois seleciono só as com 15 válidos
    for bloco in candidatos:
        nums = re.findall(r"\b(?:[01]?\d|2[0-5])\b", bloco)
        # dedup preservando ordem (evita repetições por variação do layout)
        seen = []
        for n in nums:
            if n not in seen:
                seen.append(n)
        if len(seen) >= 15:
            dezenas = [int(x) for x in seen[:15]]
            concursos.append({
                "concurso": None,  # sem número assegurado nesta heurística
                "data": None,
                "dezenas": dezenas
            })

    return concursos


# --------- rotas ----------
@app.get("/", response_class=JSONResponse)
def root():
    return {"ok": True, "message": "Lotofácil API online. Use /health ou /lotofacil?months=3", "docs": "/docs"}


@app.get("/health", response_class=JSONResponse)
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug", response_class=JSONResponse)
def debug(page: int = Query(1, ge=1)):
    """
    Faz um GET via ScraperAPI na página de resultados da Lotofácil do AS Loterias.
    Mostra status_code, tamanho do HTML e um pequeno snippet (primeiros 500 chars).
    """
    # caminho correto de resultados (plural)
    url = "https://asloterias.com.br/resultados/lotofacil"
    if page > 1:
        url = f"{url}?page={page}"

    try:
        status, html, meta = fetch_via_scraper(
            url, render=True, raise_for_status=False)
        snippet = html[:500] if html else ""
        qtd = len(html or "")
        print(f"[DEBUG] GET {url} => {status}, len={qtd}")
        return {
            "page": page,
            "url": url,
            "status_code": status,
            "qtd": qtd,
            "snippet": snippet,
            **meta,
        }
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": "debug_failed", "detail": str(e)}
        )


@app.get("/fetch", response_class=PlainTextResponse)
def fetch_raw(page: int = Query(1, ge=1)):
    """
    (Somente para teste) Devolve o HTML bruto obtido via ScraperAPI.
    Útil para você inspecionar se o conteúdo real está chegando.
    """
    url = "https://asloterias.com.br/resultados/lotofacil"
    if page > 1:
        url = f"{url}?page={page}"
    status, html, _ = fetch_via_scraper(
        url, render=True, raise_for_status=False)
    print(f"[FETCH] GET {url} => {status}, len={len(html or '')}")
    return html or ""


@app.get("/lotofacil", response_class=JSONResponse)
def lotofacil(months: int = Query(3, ge=1)):
    """
    Coleta os resultados (apenas amostra heurística) dos últimos 'months' meses,
    usando a página de resultados do AS Loterias via ScraperAPI.
    """
    try:
        # Para simplificar, baixo apenas a 1ª página (melhore depois se quiser paginação real)
        url = "https://asloterias.com.br/resultados/lotofacil"
        status, html, meta = fetch_via_scraper(
            url, render=True, raise_for_status=False)
        print(f"[AS] GET {url} => {status}, len={len(html or '')}")

        if status >= 400 or not html:
            return JSONResponse(
                status_code=502,
                content={"detail": f"AS Loterias respondeu {status} em {url}"}
            )

        concursos = parse_asloterias_list(html)

        # NOTE: aqui não estamos filtrando por “months” (sem datas confiáveis na heurística).
        # Devolvo o que achou e informo quantos.
        return {"meses": months, "qtd": len(concursos), "concursos": concursos}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": "lotofacil_failed", "detail": str(e)}
        )
