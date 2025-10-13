# main.py
from __future__ import annotations

import os
import re
import time
import math
import logging
from datetime import datetime
from typing import Dict, List, Any

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# -----------------------------------------------------------
# Configuração básica
# -----------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lotofacil")

APP_NAME = "Lotofácil API"
BASE_URL = "https://asloterias.com.br"               # novo provedor
RESULT_PAGE = f"{BASE_URL}/resultado/lotofacil"      # página inicial
RESULT_PAGE_PAGED = f"{BASE_URL}/resultado/lotofacil?p={{page}}"  # paginação

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL + "/",
    "Connection": "keep-alive",
}

# Cache em memória: { key: (expires_ts, payload) }
CACHE: Dict[str, Any] = {}
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # 10 min

# -----------------------------------------------------------
# App
# -----------------------------------------------------------
app = FastAPI(title=APP_NAME, version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)


def cache_get(key: str):
    entry = CACHE.get(key)
    if not entry:
        return None
    expires_ts, data = entry
    if time.time() > expires_ts:
        CACHE.pop(key, None)
        return None
    return data


def cache_set(key: str, data: Any, ttl: int = CACHE_TTL_SECONDS):
    CACHE[key] = (time.time() + ttl, data)


# -----------------------------------------------------------
# Utilidades de parsing
# -----------------------------------------------------------
DATE_PAT = re.compile(r"(\d{2}/\d{2}/\d{4})")
CONCURSO_PAT = re.compile(r"concurso\s*#?\s*(\d+)", re.IGNORECASE)
# números (1..25) — aceita 1 ou 2 dígitos, mas depois validamos
NUM_PAT = re.compile(r"\b(\d{1,2})\b")


def parse_draw_blocks_html(html: str) -> List[Dict[str, Any]]:
    """
    Tenta extrair os concursos do HTML do AS Loterias.
    O site muda de layout de tempos em tempos, então aqui usamos
    uma estratégia tolerante:
      1) vasculha blocos que contenham as palavras 'Lotofácil' e 'Concurso'
      2) procura data no mesmo bloco
      3) extrai 15 dezenas válidas (1..25)
    """
    soup = BeautifulSoup(html, "html.parser")

    # Há páginas onde cada resultado fica dentro de um <article> ou <div>
    # com o texto do concurso. Vamos olhar vários containers possíveis:
    candidates = []
    for tag_name in ("article", "div", "section", "li"):
        candidates.extend(soup.find_all(tag_name))

    results: List[Dict[str, Any]] = []
    seen = set()

    for node in candidates:
        text = " ".join(node.get_text(" ").split())
        if ("Lotofácil" not in text and "Lotofacil" not in text) or "concurso" not in text.lower():
            continue

        # concurso
        m_conc = CONCURSO_PAT.search(text)
        if not m_conc:
            continue
        concurso = int(m_conc.group(1))

        # data
        m_date = DATE_PAT.search(text)
        if not m_date:
            # alguns layouts mostram "dd/mm/aaaa" dentro de um <time> ou pequeno <span>
            # já tentamos com regex acima; se não achar, pula
            continue
        data_str = m_date.group(1)
        try:
            data = datetime.strptime(data_str, "%d/%m/%Y").date()
        except ValueError:
            continue

        # dezenas — pegue todas no bloco e filtre por 1..25; depois selecione 15
        nums = [int(n) for n in NUM_PAT.findall(text)]
        dezenas = [n for n in nums if 1 <= n <= 25]
        # alguns blocos têm muitos números; tentamos pegar os 15 mais “próximos”
        # de alguma marcação comum (quando há bolinhas/bolões no mesmo bloco).
        if len(dezenas) < 15:
            continue

        # heurística simples: toma a primeira janela de 15 consecutivos
        # que pareçam plausíveis
        def first_window_15(seq):
            for i in range(0, len(seq) - 14):
                win = seq[i:i+15]
                if len(set(win)) == 15:   # sem repetição
                    return sorted(win)
            return None

        picked = first_window_15(dezenas)
        if not picked:
            continue

        key = (concurso, data.isoformat(), tuple(picked))
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "concurso": concurso,
            "data": data_str,
            "dezenas": picked,
        })

    # pode vir ordem invertida; vamos ordenar por concurso crescente
    results.sort(key=lambda x: x["concurso"])
    return results


def fetch_as_loterias_page(page: int) -> List[Dict[str, Any]]:
    """
    Busca uma página de resultados (paginada) no AS Loterias.
    """
    url = RESULT_PAGE if page == 1 else RESULT_PAGE_PAGED.format(page=page)
    log.info(f"[ASLoterias] GET {url}")
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"ASLoterias retornou {resp.status_code} em {url}")
    return parse_draw_blocks_html(resp.text)


def cutoff_date(months: int) -> datetime:
    today = datetime.now()
    return (today - relativedelta(months=months)).replace(hour=0, minute=0, second=0, microsecond=0)


def fetch_last_months_from_asloterias(months: int, max_pages: int = 60) -> List[Dict[str, Any]]:
    """
    Percorre páginas até coletar concursos cujo 'data' >= data limite (meses atrás).
    'max_pages' é uma sanidade para evitar percorrer infinito se layout mudar.
    """
    if months < 1:
        months = 1
    if months > 36:
        months = 36  # limite de segurança

    limit = cutoff_date(months)
    all_draws: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        page_draws = fetch_as_loterias_page(page)
        if not page_draws:
            break

        # filtrar por data
        keep_this_page = False
        for d in page_draws:
            try:
                d_date = datetime.strptime(d["data"], "%d/%m/%Y")
            except ValueError:
                continue
            if d_date >= limit:
                keep_this_page = True
                all_draws.append(d)
        # se NENHUM concurso da página é novo, podemos parar
        if not keep_this_page:
            break

    # ordenar por concurso crescente e remover duplicatas por concurso
    uniq: Dict[int, Dict[str, Any]] = {}
    for d in all_draws:
        uniq[d["concurso"]] = d
    ordered = [uniq[k] for k in sorted(uniq)]
    return ordered


# -----------------------------------------------------------
# Endpoints
# -----------------------------------------------------------
@app.get("/")
def root():
    return {"ok": True, "message": f"{APP_NAME} online. Use /health ou /lotofacil?months=3", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok", "message": f"{APP_NAME} online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    """Depuração: baixa 1 página e mostra quantos concursos conseguiu extrair."""
    url = RESULT_PAGE if page == 1 else RESULT_PAGE_PAGED.format(page=page)
    r = requests.get(url, headers=HEADERS, timeout=20)
    draws = parse_draw_blocks_html(r.text) if r.status_code == 200 else []
    return {
        "page": page,
        "url": url,
        "status_code": r.status_code,
        "qtd": len(draws),
        "sample": draws[:2],
    }


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1, le=36)):
    """
    Retorna os concursos dos últimos N meses coletados do AS Loterias.
    Ex.: /lotofacil?months=3
    """
    cache_key = f"asloterias:lotofacil:{months}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        draws = fetch_last_months_from_asloterias(months)
    except HTTPException as e:
        # repassa erro HTTP com a mensagem amigável
        raise e
    except Exception as e:
        log.exception("Falha ao coletar AS Loterias")
        raise HTTPException(
            status_code=502, detail=f"Falha ao coletar dados do AS Loterias: {e}")

    payload = {
        "meses": months,
        "qtd": len(draws),
        "concursos": draws,
    }
    cache_set(cache_key, payload)
    return payload
