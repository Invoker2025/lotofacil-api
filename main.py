from __future__ import annotations

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ------------------------------------------------------------------------------
# Configurações gerais
# ------------------------------------------------------------------------------
UTC = timezone.utc
LOG = logging.getLogger("lotofacil")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

# Caminho correto (plural):
AS_BASE = "https://asloterias.com.br/resultados/lotofacil"

# Cabeçalhos para parecer navegação de browser
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://asloterias.com.br/",
}

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)
SESSION.timeout = 25  # segundos

# ------------------------------------------------------------------------------
# App
# ------------------------------------------------------------------------------
app = FastAPI(title="Lotofácil API (AS Loterias)", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Utilidades de parsing
# ------------------------------------------------------------------------------

_CONCURSO_RE = re.compile(r"(concurso|conc\.)\s*#?\s*(\d+)", re.I)
# datas como 12/10/2025
_DATA_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
# sequências de 15 números (dois dígitos ou um dígito), tolerante
_NUMS_RE = re.compile(r"\b(\d{1,2})\b")


def _parse_date_any(text: str) -> datetime | None:
    """Tenta encontrar uma data dd/mm/aaaa no texto, senão usa dateutil."""
    m = _DATA_RE.search(text or "")
    if m:
        try:
            return dtparser.parse(m.group(1), dayfirst=True).replace(tzinfo=UTC)
        except Exception:
            pass
    try:
        return dtparser.parse(text, dayfirst=True).replace(tzinfo=UTC)
    except Exception:
        return None


def _extract_15_numbers(container: str) -> List[int]:
    """
    Extrai 15 dezenas do HTML/texto do card do concurso.
    Estratégia: encontrarmos o bloco do card e capturarmos 15 inteiros únicos (1..25).
    """
    nums = [int(x) for x in _NUMS_RE.findall(container)]
    # Mantém apenas dezenas válidas 1..25
    nums = [n for n in nums if 1 <= n <= 25]
    # Heurística: pega a primeira sequência de 15 valores
    seq: List[int] = []
    for n in nums:
        if len(seq) < 15:
            seq.append(n)
        if len(seq) == 15:
            break
    return seq if len(seq) == 15 else []


def _extract_card_info(card: BeautifulSoup) -> Dict[str, Any] | None:
    """
    Extrai {concurso, data, dezenas} de um 'card' da página.
    A AS Loterias muda markup com alguma frequência; deixamos heurístico/tolerante.
    """
    raw = card.get_text(" ", strip=True) if card else ""
    html = str(card)

    # concurso
    conc = None
    m = _CONCURSO_RE.search(raw)
    if m:
        conc = int(m.group(2))

    # data
    data = _parse_date_any(raw)

    # dezenas
    dezenas = _extract_15_numbers(html)

    if conc and data and dezenas:
        return {
            "concurso": conc,
            "data": data.date().isoformat(),
            "dezenas": dezenas,
        }
    return None


def _page_url(page: int) -> str:
    # a AS Loterias pagina por querystring ?pagina=N em alguns sites; aqui testamos
    # dois formatos conhecidos: /resultados/lotofacil e com ?page=N.
    return AS_BASE if page == 1 else f"{AS_BASE}?page={page}"


def _fetch_page(page: int) -> Tuple[int, str]:
    url = _page_url(page)
    LOG.info(f"[AS] GET {url}")
    r = SESSION.get(url)
    return r.status_code, r.text


def _parse_list_page(html: str) -> List[Dict[str, Any]]:
    """
    Varre a página e tenta localizar 'cards' de resultados.
    Padrões comuns:
      - <article> por concurso
      - divs com classes que incluem 'card' / 'resultado' / 'draw' etc.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) candidatos óbvios de cards
    candidates = []
    candidates += soup.select("article")
    candidates += soup.select("div.card, div.resultado, div.result, li, section")

    # remove duplicados mantendo ordem
    seen = set()
    uniq = []
    for el in candidates:
        key = id(el)
        if key not in seen:
            seen.add(key)
            uniq.append(el)

    results: List[Dict[str, Any]] = []
    for el in uniq:
        info = _extract_card_info(el)
        if info:
            results.append(info)

    # fallback mais amplo: procurar blocos com 15 números em qualquer container
    if not results:
        for el in soup.find_all(True):
            info = _extract_card_info(el)
            if info:
                results.append(info)

    # remove duplicados de concurso
    dedup = {}
    for it in results:
        dedup[it["concurso"]] = it
    ordered = sorted(dedup.values(), key=lambda x: x["concurso"], reverse=True)
    return ordered

# ------------------------------------------------------------------------------
# Coletor por meses (paginação até data limite)
# ------------------------------------------------------------------------------


def collect_lotofacil_from_as(months: int) -> Dict[str, Any]:
    if months < 1:
        months = 1

    cutoff = (datetime.now(tz=UTC) -
              timedelta(days=30 * months)).date().isoformat()
    LOG.info(f"[AS] Coletando até a data (aprox) {cutoff}  (~{months} meses)")

    page = 1
    out: List[Dict[str, Any]] = []
    older_reached = False
    max_pages = 60  # segurança

    while page <= max_pages and not older_reached:
        status, html = _fetch_page(page)
        if status != 200:
            # quando 404, paramos
            LOG.warning(f"[AS] status {status} page={page}; interrompendo.")
            break

        cards = _parse_list_page(html)
        LOG.info(f"[AS] page={page} -> {len(cards)} cartões")

        if not cards:
            # nada encontrado nesta página; paramos
            break

        for c in cards:
            out.append(c)
            if c["data"] <= cutoff:
                older_reached = True

        page += 1
        # evita ser agressivo
        time.sleep(0.6)

    # filtra por cutoff e ordena do mais recente para o mais antigo
    filtered = [c for c in out if c["data"] > cutoff]
    filtered = sorted(filtered, key=lambda x: (
        x["data"], x["concurso"]), reverse=True)

    return {
        "meses": months,
        "qtd": len(filtered),
        "concursos": filtered,
    }


# ------------------------------------------------------------------------------
# Cache simples
# ------------------------------------------------------------------------------
CACHE: Dict[int, Dict[str, Any]] = {}


def warm_cache():
    for m in [1, 2, 3, 4, 5, 6, 12]:
        try:
            CACHE[m] = collect_lotofacil_from_as(m)
            LOG.info(
                f"[CACHE] Atualizado para {m}m: {CACHE[m]['qtd']} concursos")
        except Exception as e:
            LOG.exception(f"[CACHE] Falha ao atualizar {m}m: {e}")

# ------------------------------------------------------------------------------
# Rotas
# ------------------------------------------------------------------------------


@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health ou /lotofacil?months=3",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    """Baixa 1 página e mostra um sample básico para validar scraping."""
    status, html = _fetch_page(page)
    if status != 200:
        return {"page": page, "url": _page_url(page), "status_code": status, "qtd": 0, "sample": []}
    parsed = _parse_list_page(html)
    return {
        "page": page,
        "url": _page_url(page),
        "status_code": status,
        "qtd": len(parsed),
        "sample": parsed[:2],
    }


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1)):
    # cache rápido
    if months in CACHE and CACHE[months].get("qtd", 0) > 0:
        return CACHE[months]
    try:
        data = collect_lotofacil_from_as(months)
        # guarda no cache só se tiver algo
        if data.get("qtd", 0) > 0:
            CACHE[months] = data
        return data
    except Exception as e:
        LOG.exception("Erro coletando AS Loterias")
        raise HTTPException(status_code=502, detail=str(e))

# ------------------------------------------------------------------------------
# Startup: aquece o cache
# ------------------------------------------------------------------------------


@app.on_event("startup")
def on_startup():
    LOG.info("Iniciando app; aquecendo cache…")
    try:
        warm_cache()
    except Exception:
        LOG.exception("Falha no warm_cache()")


# ------------------------------------------------------------------------------
# Exec local
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8900"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
