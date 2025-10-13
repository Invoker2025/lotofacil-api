import os
import re
import time
import json
from datetime import datetime, timedelta
import typing as T

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup

# ============================================
# CONFIGURAÇÕES
# ============================================
SCRAPER_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
if not SCRAPER_KEY:
    print("[ERRO] Variável de ambiente SCRAPERAPI_KEY não encontrada!")

BASE_URL = "https://www.sorteonline.com.br/lotofacil/resultados"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

REQUEST_TIMEOUT = 60
HARD_DEADLINE = 45  # tempo máximo total por request

# ============================================
# INICIALIZA FASTAPI
# ============================================
app = FastAPI(
    title="Lotofácil API - SorteOnline + ScraperAPI",
    version="3.1.0",
    description="Extrai resultados da Lotofácil do site SorteOnline via ScraperAPI (render + premium + BR).",
)

# ============================================
# FUNÇÃO DE REQUISIÇÃO AO SCRAPERAPI
# ============================================


def _scraper_get(url: str, render: bool = True, retries: int = 3) -> requests.Response:
    """
    Faz GET via ScraperAPI com renderização JS e suporte a Cloudflare.
    """
    params = {
        "api_key": SCRAPER_KEY,
        "url": url,
        "render": "true" if render else "false",
        "country_code": "br",
        "keep_headers": "true",
        "premium": "true",         # necessário para Cloudflare
        "ultra_premium": "true",   # reforço para render pesado
        "timeout": str(60000),
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.sorteonline.com.br/",
    }

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                "https://api.scraperapi.com/",
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            # Se o ScraperAPI retornar erro explícito
            if resp.status_code >= 500 or "Request failed" in resp.text:
                time.sleep(1.5 * attempt)
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * attempt)

    if last_exc:
        raise last_exc
    raise RuntimeError("Falha desconhecida no ScraperAPI")

# ============================================
# PARSER DOS RESULTADOS
# ============================================


def parse_results(html: str) -> T.List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []

    for div in soup.find_all("div"):
        text = div.get_text(" ", strip=True)
        if "Concurso" in text and re.search(r"\b\d{2}\b(?:\s*,\s*\d{2}){14}", text):
            cards.append(text)

    resultados = []
    for txt in cards:
        m_conc = re.search(r"Concurso[:\s]+(\d{3,5})", txt)
        m_data = re.search(r"(\d{2}[/-]\d{2}[/-]\d{4})", txt)
        m_dezenas = re.search(r"(\d{2}(?:\s*,\s*\d{2}){14})", txt)

        if not (m_conc and m_dezenas):
            continue

        dezenas = [int(x) for x in re.split(r"\s*,\s*", m_dezenas.group(1))]
        data_iso = None
        if m_data:
            try:
                data_iso = datetime.strptime(m_data.group(1).replace(
                    "-", "/"), "%d/%m/%Y").strftime("%Y-%m-%d")
            except Exception:
                pass

        resultados.append({
            "concurso": int(m_conc.group(1)),
            "data": data_iso,
            "dezenas": dezenas
        })

    return resultados

# ============================================
# ENDPOINTS
# ============================================


@app.get("/health")
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    url = f"{BASE_URL}?pagina={page}"
    try:
        resp = _scraper_get(url)
        snippet = resp.text[:1200]
        return {
            "url": url,
            "status_code": resp.status_code,
            "reason": resp.reason,
            "body_trunc": snippet,
            "headers_used": {"User-Agent": USER_AGENT, "Referer": "https://www.sorteonline.com.br/"},
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro no ScraperAPI: {e}")


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1, le=12)):
    deadline = time.time() + HARD_DEADLINE
    limite_data = (datetime.utcnow() - timedelta(days=30 * months)).date()
    pagina = 1
    resultados = []

    while time.time() < deadline:
        url = f"{BASE_URL}?pagina={pagina}"
        try:
            resp = _scraper_get(url)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"Erro ao acessar SorteOnline: {e}")

        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code,
                                detail=f"SorteOnline respondeu {resp.status_code}")

        parsed = parse_results(resp.text)
        if not parsed:
            break
        resultados.extend(parsed)

        mais_antigo = min(
            (datetime.strptime(r["data"], "%Y-%m-%d").date()
             for r in parsed if r["data"]),
            default=None,
        )
        if mais_antigo and mais_antigo <= limite_data:
            break

        pagina += 1
        time.sleep(0.8)

    unicos = {r["concurso"]: r for r in resultados}
    final = list(unicos.values())
    final.sort(key=lambda x: x["concurso"], reverse=True)

    return {"meses": months, "qtd": len(final), "concursos": final}
