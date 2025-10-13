import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, HTTPException

APP_NAME = "Lotofácil API (AS Loterias)"
VERSION = "2.1.0"

# ----- Configuração do upstream (AS Loterias) -----
# >>> AQUI ESTAVA O PROBLEMA: deve ser 'resultado' (singular)
BASE_URL = "https://asloterias.com.br/resultado/lotofacil"

# ----- ScraperAPI (opcional) -----
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")  # defina no Render > Environment
SCRAPERAPI_URL = "https://api.scraperapi.com"


def http_get(url: str, params: Dict[str, Any] | None = None, headers: Dict[str, str] | None = None) -> requests.Response:
    """Faz GET direto ou via ScraperAPI (se a chave existir)."""
    params = dict(params or {})
    headers = dict(headers or {})

    if SCRAPERAPI_KEY:
        # usamos ScraperAPI
        payload = {"api_key": SCRAPERAPI_KEY,
                   "url": url, "keep_headers": "true"}
        # Opcional: passar cabeçalhos de um navegador
        headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/128.0.0.0 Safari/537.36")
        resp = requests.get(SCRAPERAPI_URL, params=payload,
                            headers=headers, timeout=30)
    else:
        # requisição direta
        headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/128.0.0.0 Safari/537.36")
        resp = requests.get(url, params=params, headers=headers, timeout=30)

    return resp


# ----- FastAPI -----
app = FastAPI(title=APP_NAME, version=VERSION)


@app.get("/")
def root():
    return {"ok": True, "message": "Lotofácil API online. Use /health ou /lotofacil?months=3", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    """
    Baixa a página pedida do AS Loterias e retorna status + pequena amostra.
    Útil para checar se a ScraperAPI está funcionando (status_code deve ser 200).
    """
    url = BASE_URL
    if page > 1:
        # AS Loterias usa paginação adicionada após a URL com ?page=N
        url = f"{BASE_URL}?page={page}"

    resp = http_get(url)
    sample = []
    if resp.status_code == 200:
        # devolve só os 200 primeiros caracteres de HTML para conferência
        snippet = resp.text[:200]
        sample.append(snippet)

    return {
        "page": page,
        "url": url,
        "status_code": resp.status_code,
        "qtd": len(sample),
        "sample": sample
    }


# ----------------- PARSER -----------------
date_re = re.compile(r"(\d{2}/\d{2}/\d{4})")
num_re = re.compile(r"\b\d{2}\b")


def parse_draws_from_html(html: str) -> List[Dict[str, Any]]:
    """
    Tenta extrair sorteios da página do AS Loterias.
    Estratégia robusta: procurar blocos com data e 15 números (dois dígitos).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Heurística: cada sorteio costuma estar em um card/box; vamos olhar por seções/divs
    blocks = soup.find_all(["section", "article", "div"])
    results: List[Dict[str, Any]] = []

    for b in blocks:
        text = " ".join(b.stripped_strings)
        if not text:
            continue

        # Procura uma data dentro do bloco
        dmatch = date_re.search(text)
        if not dmatch:
            continue
        date_str = dmatch.group(1)

        # Procura números de dois dígitos dentro do bloco
        nums = num_re.findall(text)
        # Heurística: sorteio da Lotofácil tem 15 dezenas; pegamos a primeira sequência de 15
        if len(nums) >= 15:
            # Filtra só os 15 primeiros (para não pegar extras de outros textos)
            dezenas = list(map(int, nums[:15]))
            results.append({
                "data": date_str,
                "dezenas": dezenas
            })

    return results


def load_months(months: int) -> List[Dict[str, Any]]:
    """
    Percorre as páginas do AS Loterias até cobrir o intervalo de 'months'
    (ou até acabarem os resultados).
    """
    since = datetime.today() - timedelta(days=30 * months)
    page = 1
    found: List[Dict[str, Any]] = []

    while True:
        url = BASE_URL if page == 1 else f"{BASE_URL}?page={page}"
        resp = http_get(url)

        # Se der 403/404 etc, paramos
        if resp.status_code != 200:
            break

        draws = parse_draws_from_html(resp.text)
        if not draws:
            break  # nada reconhecido nessa página

        # Coleta apenas os dentro do intervalo
        added_this_page = 0
        for d in draws:
            try:
                ddate = datetime.strptime(d["data"], "%d/%m/%Y")
            except Exception:
                continue
            if ddate >= since:
                found.append(d)
                added_this_page += 1

        # Se não adicionou nada nessa página, provavelmente já passou do limite
        if added_this_page == 0:
            break

        page += 1
        if page > 50:  # trava de segurança
            break

    return found


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1)):
    """
    Retorna todos os concursos dos últimos 'months' meses (extraídos do AS Loterias).
    """
    try:
        data = load_months(months)
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Falha ao coletar dados: {e}")

    return {
        "meses": months,
        "qtd": len(data),
        "concursos": data
    }
