import os
import re
import math
import json
import time
import random
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

APP_NAME = "Lotofácil API (AS Loterias)"
app = FastAPI(title=APP_NAME, version="2.1.0")

# ---------- Config ----------
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
USE_PROXY = bool(SCRAPERAPI_KEY)

SESSION = requests.Session()
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://asloterias.com.br/",
}

# Candidatos de URL: vamos tentar até achar um 200
BASES = [
    "https://asloterias.com.br/resultados/lotofacil",
    "https://asloterias.com.br/resultados/lotofacil/",
    "https://asloterias.com.br/resultado/lotofacil",
    "https://asloterias.com.br/resultado/lotofacil/",
]

# Formas de paginar que já vi em sites brasileiros
PAG_PATTERNS = [
    lambda base, p: f"{base}?pagina={p}",
    lambda base, p: f"{base}?page={p}",
    lambda base, p: f"{base}pagina-{p}/" if base.endswith(
        "/") else f"{base}/pagina-{p}/",
    lambda base, p: f"{base}{p}/" if base.endswith("/") else f"{base}/{p}/",
]

# cache simples de detecção de URL boa (sobrevive até reiniciar o dyno)
DETECTED = {"base": None, "pattern_idx": None, "ts": 0.0}


def _scraper_get(url: str, timeout: int = 20) -> requests.Response:
    """Faz GET usando ScraperAPI (se houver chave) ou direto."""
    if USE_PROXY:
        params = {
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            # render ajuda quando o site gera parte via JS; se não precisar, tudo bem
            "render": "true",
            # forwarda cabeçalhos comuns
            "keep_headers": "true",
        }
        prox = f"https://api.scraperapi.com/?{urlencode(params)}"
        return SESSION.get(prox, headers=DEFAULT_HEADERS, timeout=timeout)
    else:
        return SESSION.get(url, headers=DEFAULT_HEADERS, timeout=timeout)


def _try_detect_working(page: int = 1) -> Tuple[str, int, List[Dict]]:
    """
    Tenta todas as combinações de base + paginação até obter HTTP 200.
    Retorna (url_ok, patt_idx, attempts)
    """
    attempts = []

    # se já detectamos nos últimos 12h, reaproveita
    if DETECTED["base"] and time.time() - DETECTED["ts"] < 12 * 3600:
        base = DETECTED["base"]
        patt_idx = DETECTED["pattern_idx"]
        url = PAG_PATTERNS[patt_idx](base, page)
        r = _scraper_get(url)
        attempts.append({"url": url, "status": r.status_code})
        if r.status_code == 200 and "lotofácil" in r.text.lower():
            return url, patt_idx, attempts  # válido
        # caso tenha quebrado, limpa detecção e segue para varredura
        DETECTED["base"] = None

    # varre
    for base in BASES:
        # primeiro testa a home da base (sem pagina)
        r0 = _scraper_get(base)
        attempts.append({"url": base, "status": r0.status_code})
        ok_base = r0.status_code == 200

        for i, patt in enumerate(PAG_PATTERNS):
            url = patt(base, page)
            r = _scraper_get(url)
            attempts.append({"url": url, "status": r.status_code})
            if r.status_code == 200 and (("lotofacil" in r.text.lower()) or ("lotofácil" in r.text.lower())):
                DETECTED["base"] = base
                DETECTED["pattern_idx"] = i
                DETECTED["ts"] = time.time()
                return url, i, attempts

        # às vezes a base já é a própria página 1
        if ok_base and (("lotofacil" in r0.text.lower()) or ("lotofácil" in r0.text.lower())):
            DETECTED["base"] = base
            DETECTED["pattern_idx"] = -1  # sem padronização de pagina
            DETECTED["ts"] = time.time()
            return base, -1, attempts

    # nada funcionou
    return "", -2, attempts


def _url_for(page: int) -> str:
    """Constrói URL usando a detecção anterior; se não tiver, detecta."""
    if not DETECTED["base"]:
        url, patt_idx, _ = _try_detect_working(page=page)
        if not url:
            return ""  # deixamos o caller tratar
        return url

    base = DETECTED["base"]
    idx = DETECTED["pattern_idx"]
    if idx is None or idx == -1:
        return base
    return PAG_PATTERNS[idx](base, page)


def _parse_draws(html: str) -> List[Dict]:
    """
    Parser dos concursos no ASLoterias (genérico):
    - Tenta encontrar blocos com dezenas (15) e número de concurso/data.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # Cada concurso costuma ter um container com título contendo "Concurso"
    # e uma lista de 15 dezenas.
    # Vamos procurar títulos e, a partir deles, capturar as dezenas próximas.
    cards = soup.select("article, div, section")
    for c in cards:
        text = " ".join(c.stripped_strings).lower()
        if "concurso" in text and any(k in text for k in ["lotofacil", "lotofácil"]):
            # capturar dezenas (números de 1..25)
            nums = [int(m) for m in re.findall(
                r"\b([01]?\d|2[0-5])\b", c.get_text()) if 1 <= int(m) <= 25]
            # queremos exatamente 15 dezenas, mas o container pode trazer extras (data, nº concurso etc.)
            # estratégia: procurar a subsequência de 15 números entre 1..25 mais provável
            best = []
            for i in range(len(nums) - 14):
                chunk = nums[i:i+15]
                if all(1 <= x <= 25 for x in chunk):
                    # filtro simples: não permitir repetições no chunk
                    if len(set(chunk)) == 15:
                        best = chunk
                        break
            if best:
                # tentar extrair número do concurso
                m = re.search(r"concurso\s*([0-9]{3,5})", c.get_text(), re.I)
                conc = int(m.group(1)) if m else None
                items.append({"concurso": conc, "dezenas": best})

    # remover duplicados por concurso
    seen = set()
    uniq = []
    for it in items:
        key = tuple(it["dezenas"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def _collect_months(months: int) -> Tuple[List[Dict], List[Dict]]:
    """
    Coleta páginas até cobrir ~months meses (aprox).
    Como regra simples, usamos ~18 concursos/mês.
    """
    per_month = 18
    target = max(1, months) * per_month

    all_items: List[Dict] = []
    attempts_log: List[Dict] = []

    page = 1
    guard = 0
    while len(all_items) < target and guard < 40:  # limite de segurança
        guard += 1
        url = _url_for(page)
        if not url:
            break

        r = _scraper_get(url)
        attempts_log.append({"url": url, "status": r.status_code})

        if r.status_code != 200:
            # falhou; força nova detecção e tenta próxima
            DETECTED["base"] = None
            page += 1
            continue

        draws = _parse_draws(r.text)
        if not draws:
            # pode ser uma página “vazia” ou formato diferente
            page += 1
            continue

        # agrega (evitando duplicados pelo concurso)
        seen_conc = {d.get("concurso") for d in all_items if d.get("concurso")}
        for d in draws:
            if d.get("concurso") and d["concurso"] in seen_conc:
                continue
            all_items.append(d)

        page += 1

    return all_items, attempts_log


# ---------- Endpoints ----------

@app.get("/")
def root():
    return {"ok": True, "message": "Lotofácil API online. Use /health ou /lotofacil?months=3", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    """Mostra todas as tentativas de URL para a página pedida e um trecho HTML."""
    url, patt_idx, attempts = _try_detect_working(page=page)
    if not url:
        return JSONResponse(
            status_code=404,
            content={"page": page, "url": None, "status_code": 404,
                     "attempts": attempts, "sample": []},
        )
    r = _scraper_get(url)
    snippet = r.text[:1000] if r.status_code == 200 else ""
    return {"page": page, "url": url, "status_code": r.status_code, "attempts": attempts, "sample": snippet[:300]}


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1)):
    """Coleta ~N meses de concursos na ASLoterias (via tentativa automática de URL)."""
    contests, attempts = _collect_months(months)
    if not contests:
        raise HTTPException(
            status_code=502,
            detail=f"Não foi possível coletar na ASLoterias com as tentativas atuais. Veja /debug?page=1 para detalhes.",
        )

    # Regras que você pediu (6 pares + 6 ímpares dos mais frequentes, etc.) podem ser reaplicadas aqui depois.
    return {"meses": months, "qtd": len(contests), "concuros": contests, "attempts": attempts[:10]}
