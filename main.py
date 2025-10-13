import os
import re
import time
import math
import json
import typing as T
from datetime import datetime, timedelta

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

# ------------------------------
# Config
# ------------------------------
SCRAPER_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
if not SCRAPER_KEY:
    print("[WARN] SCRAPERAPI_KEY não definido no ambiente.")

BASE_SO_URL = "https://www.sorteonline.com.br/lotofacil/resultados"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# limites para não estourar proxy da Render
REQUEST_TIMEOUT = 60  # requests -> ScraperAPI
HARD_DEADLINE = 45    # tempo máximo de scraping total por request da nossa API

# ------------------------------
# FastAPI
# ------------------------------
app = FastAPI(
    title="Lotofácil API (SorteOnline via ScraperAPI)",
    version="3.0.0",
    contact={"name": "Invoker2025"},
)

# ------------------------------
# Helpers de rede
# ------------------------------


def _scraper_get(url: str, *, render: bool = True, retries: int = 4, backoff: float = 1.5) -> requests.Response:
    """
    Faz GET via ScraperAPI com renderização JS, BR e headers preservados.
    Tenta novamente em 429/5xx com backoff.
    """
    if not SCRAPER_KEY:
        raise RuntimeError("SCRAPERAPI_KEY ausente nas variáveis de ambiente.")

    params = {
        "api_key": SCRAPER_KEY,
        "url": url,
        "render": "true",
        "country_code": "br",
        "keep_headers": "true",
        "premium": "true",        # <- ativa renderização JS completa
        "ultra_premium": "true",  # <- se seu plano permitir, garante bypass Cloudflare
        "timeout": str(60000),
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.sorteonline.com.br/",
    }

    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                "https://api.scraperapi.com/",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            # ScraperAPI devolve 200 mesmo quando o alvo devolve 403/404 etc.
            # Vamos checar o status_code real do ScraperAPI:
            if resp.status_code >= 500 or resp.status_code == 429:
                # backoff em HTTP do proxy
                time.sleep(backoff ** attempt)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(backoff ** attempt)

    if last_exc:
        raise last_exc
    raise RuntimeError("Falha desconhecida no ScraperAPI")


# ------------------------------
# Parser SorteOnline (robusto)
# ------------------------------
RESULT_CARD_SEL = "article,div"  # fallback amplo


def _parse_result_list(html: str) -> T.List[dict]:
    """
    Tenta extrair uma lista de concursos a partir do HTML do SorteOnline.
    Estruturas mudam com frequência, então o parser é tolerante.
    Retorno: [{concurso:int, data:'YYYY-MM-DD', dezenas:[...]}]
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) encontrar "cards" de resultados (divs/artigos com texto 'Concurso' e dezenas)
    cards = []
    for node in soup.select(RESULT_CARD_SEL):
        txt = node.get_text(" ", strip=True)
        if not txt:
            continue
        if "Concurso" in txt and re.search(r"\b\d{2}\b(?:\s*,\s*\d{2}){14}", txt):
            cards.append(node)

    resultados: T.List[dict] = []
    for card in cards:
        text = card.get_text(" ", strip=True)

        # concurso: "Concurso 3201" ou "Concurso: 3201"
        m_conc = re.search(r"Concurso[:\s]+(\d{3,5})", text, flags=re.I)
        if not m_conc:
            continue
        concurso = int(m_conc.group(1))

        # data: formatos comuns "12/10/2025" ou "12-10-2025"
        m_dt = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", text)
        data_iso = None
        if m_dt:
            try:
                d = datetime.strptime(m_dt.group(
                    1).replace("-", "/"), "%d/%m/%Y")
                data_iso = d.strftime("%Y-%m-%d")
            except Exception:
                pass

        # dezenas: 15 números de 01 a 25
        dezenas_match = re.search(r"\b(\d{2}(?:\s*,\s*\d{2}){14})\b", text)
        dezenas = []
        if dezenas_match:
            dezenas = [int(x) for x in re.split(
                r"\s*,\s*", dezenas_match.group(1))]

        if dezenas:
            resultados.append(
                {"concurso": concurso, "data": data_iso, "dezenas": dezenas}
            )

    # Remover duplicados (se houver)
    dedup = {}
    for r in resultados:
        dedup[r["concurso"]] = r
    out = list(dedup.values())
    out.sort(key=lambda x: x["concurso"], reverse=True)
    return out

# ------------------------------
# Endpoints
# ------------------------------


@app.get("/", summary="Root")
def root():
    return {"ok": True, "message": "Lotofácil API online. Use /health, /debug?page=1 ou /lotofacil?months=3", "docs": "/docs"}


@app.get("/health", summary="Health")
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug", summary="Debug")
def debug(page: int = Query(1, ge=1)):
    """
    Baixa a página e retorna status HTTP, tamanho e um trecho do HTML
    pra você validar no navegador. Útil pra testar bloqueios/timeouts.
    """
    url = f"{BASE_SO_URL}?pagina={page}"
    t0 = time.time()
    try:
        resp = _scraper_get(url, render=True)
        elapsed = round(time.time() - t0, 3)
        snippet = resp.text[:1200]
        return JSONResponse(
            {
                "page": page,
                "url": url,
                "status_code": resp.status_code,
                "elapsed_s": elapsed,
                "qtd": len(resp.text),
                "snippet": snippet,
                "headers_used": {
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Referer": "https://www.sorteonline.com.br/",
                },
            }
        )
    except requests.ReadTimeout:
        raise HTTPException(
            status_code=504, detail="Timeout renderizando página (ScraperAPI).")
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Erro no ScraperAPI: {exc}")


@app.get("/lotofacil", summary="Resultados da Lotofácil (últimos N meses)")
def lotofacil(months: int = Query(3, alias="months", ge=1, le=12)):
    """
    Percorre páginas do SorteOnline via ScraperAPI até cobrir 'months' meses
    (ou até o limite de tempo da Render). Retorna concursos parseds.
    """
    deadline = time.time() + HARD_DEADLINE
    limite_data = (datetime.utcnow() - timedelta(days=30 * months)).date()

    pagina = 1
    todos: T.List[dict] = []

    while time.time() < deadline:
        url = f"{BASE_SO_URL}?pagina={pagina}"
        try:
            resp = _scraper_get(url, render=True)
        except requests.ReadTimeout:
            raise HTTPException(
                status_code=504, detail="Timeout renderizando página (ScraperAPI).")
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Erro no ScraperAPI: {exc}")

        if resp.status_code >= 500:
            raise HTTPException(
                status_code=502, detail=f"SorteOnline respondeu {resp.status_code} em {url}")

        # Parse
        page_results = _parse_result_list(resp.text)
        if not page_results:
            # Nada encontrado nesta página -> encerra
            break

        todos.extend(page_results)

        # Verifica se já cobrimos o recorte de meses
        mais_antigo = None
        for r in page_results:
            if r.get("data"):
                d = datetime.strptime(r["data"], "%Y-%m-%d").date()
                mais_antigo = d if (mais_antigo is None or d <
                                    mais_antigo) else mais_antigo
        if mais_antigo and mais_antigo <= limite_data:
            break

        pagina += 1

        # Breve pausa pra não parecer bot agressivo
        time.sleep(0.8)

    # Dedup / Sort final
    uniq = {}
    for r in todos:
        uniq[r["concurso"]] = r
    final = list(uniq.values())
    final.sort(key=lambda x: x["concurso"], reverse=True)

    return {"meses": months, "qtd": len(final), "concursos": final}
