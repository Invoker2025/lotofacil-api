import os
import re
import time
import math
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------
# Configurações
# --------------------------------------------------------------------
SORTEONLINE_URL = "https://www.sorteonline.com.br/lotofacil/resultados"
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
TIMEOUT = 30  # segundos
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://www.sorteonline.com.br/",
}

app = FastAPI(title="Lotofácil API (SorteOnline)", version="2.1.0")

# cache simples em memória para reduzir scraping repetido
_CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 60  # 1 min

# --------------------------------------------------------------------
# Helpers de HTTP (ScraperAPI + fallback)
# --------------------------------------------------------------------


def _scrape_get(url: str, *, render: bool = True, params: Optional[Dict[str, Any]] = None) -> requests.Response:
    """
    Faz GET usando ScraperAPI se SCRAPERAPI_KEY existir; senão, faz direto.
    Para SorteOnline, recomenda-se render=True (carrega o HTML pós-JS).
    """
    params = params or {}
    if SCRAPERAPI_KEY:
        # ScraperAPI com renderização
        saparams = {
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "keep_headers": "true",
            "country_code": "br",
        }
        if render:
            saparams["render"] = "true"
        resp = requests.get(
            "https://api.scraperapi.com/",
            params=saparams,
            headers=DEFAULT_HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp
    else:
        # Fallback direto (pode não renderizar o conteúdo JS)
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp


def _cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if (time.time() - ts) > CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(key: str, value: Any):
    _CACHE[key] = (time.time(), value)


# --------------------------------------------------------------------
# Parsing dos resultados do SorteOnline (heurístico, tolerante)
# --------------------------------------------------------------------
_num_re = re.compile(r"\b([0-9]{1,2})\b")
_dt_re = re.compile(r"(\d{2}/\d{2}/\d{4})")
_conc_re = re.compile(r"[Cc]oncurso\s*#?\s*(\d+)")


def _extract_15_numbers_from_html(html_fragment: str) -> Optional[List[int]]:
    """
    Recebe um trecho de HTML (string) e tenta extrair exatamente 15 dezenas (1..25).
    Retorna lista de 15 ints ou None.
    """
    nums = [int(n) for n in _num_re.findall(html_fragment)]
    # Filtra para intervalo válido
    nums = [n for n in nums if 1 <= n <= 25]
    # Alguns blocos podem ter números da data junto; tentamos pegar o maior bloco contínuo de 15 dezenas
    if len(nums) < 15:
        return None
    # Estratégia: varrer janelas de tamanho 15 procurando sequência plausível
    for i in range(0, len(nums) - 14):
        window = nums[i: i + 15]
        # heurística mínima: não permitir números repetidos demais (no sorteio são 15 únicos)
        if len(set(window)) == 15:
            return window
    return None


def parse_sorteonline(html: str) -> List[Dict[str, Any]]:
    """
    Varre a página de resultados do SorteOnline e retorna uma lista de concursos.
    Cada item: {"concurso": int, "data": "YYYY-MM-DD", "dezenas": [..15 ints..]}
    A página normalmente contém cards de vários concursos (recentes primeiro).
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) Tentar achar cards clássicos de resultado (ul/li com 15 bolas)
    cards: List[Dict[str, Any]] = []

    # Heurística 1: qualquer bloco que contenha 15 números válidos
    # Procuramos contêineres com possíveis cabeçalhos (contendo "Concurso" e data)
    # e uma lista de dezenas perto.
    for block in soup.find_all(True):
        text = " ".join(block.get_text(" ", strip=True).split())
        if ("Concurso" in text or "concurso" in text) and _dt_re.search(text):
            # Encontrou um cabeçalho que provavelmente é um card de concurso
            # Buscamos dezenas nos descendentes
            dezenas = None
            # 1a) procurar por listas claras (li / span)
            candidates = block.find_all(["ul", "ol", "div"], recursive=True)
            for c in candidates:
                frag = str(c)
                nums = _extract_15_numbers_from_html(frag)
                if nums:
                    dezenas = nums
                    break
            if not dezenas:
                # 1b) tenta no próprio bloco
                dezenas = _extract_15_numbers_from_html(str(block))
            if dezenas:
                # concurso
                mconc = _conc_re.search(text)
                conc = int(mconc.group(1)) if mconc else None
                # data
                mdt = _dt_re.search(text)
                dt_str = mdt.group(1) if mdt else None
                dt_iso = None
                if dt_str:
                    try:
                        dt = datetime.strptime(dt_str, "%d/%m/%Y").date()
                        dt_iso = dt.isoformat()
                    except Exception:
                        dt_iso = None
                cards.append(
                    {
                        "concurso": conc,
                        "data": dt_iso,
                        "dezenas": sorted(dezenas),
                        "fonte": "sorteonline",
                    }
                )

    # Remover duplicados por concurso (mantém o 1º)
    seen = set()
    uniq = []
    for c in cards:
        key = c.get("concurso"), tuple(c.get("dezenas", []))
        if key not in seen:
            seen.add(key)
            uniq.append(c)

    # Ordena por concurso desc quando disponível, senão por data desc, senão fica como está
    def _ord_key(item):
        if item.get("concurso"):
            return (0, -int(item["concurso"]))
        elif item.get("data"):
            return (1, item["data"])
        return (2, 0)

    uniq.sort(key=_ord_key)
    return uniq


def fetch_sorteonline_page(page: int = 1) -> Dict[str, Any]:
    """
    Baixa a página de resultados. O SorteOnline não usa paginação no mesmo padrão
    em todas as épocas; então mantemos `page` para futura compatibilidade.
    """
    url = SORTEONLINE_URL
    # Algumas versões usam página única com “carregar mais” via JS. Renderização já cobre isso.
    resp = _scrape_get(url, render=True)
    html = resp.text
    concursos = parse_sorteonline(html)

    return {
        "page": page,
        "url": url,
        "qtd": len(concursos),
        "sample": concursos[:3],  # mostra alguns no debug
    }


def get_concursos_last_months(months: int) -> Dict[str, Any]:
    """
    Coleta concursos dos últimos `months` meses.
    Como a página já traz uma boa quantidade de concursos recentes, coletamos 1 página renderizada
    e filtramos por data.
    Se algum concurso não tiver data parseada, mantemos por segurança (melhor sobrar do que faltar).
    """
    cache_key = f"lotofacil:{months}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    resp = _scrape_get(SORTEONLINE_URL, render=True)
    concursos = parse_sorteonline(resp.text)

    # Filtro por meses: data >= hoje - months
    cutoff = (datetime.now(timezone.utc) - timedelta(days=months * 30)).date()

    def _keep(item):
        if item.get("data"):
            try:
                d = datetime.fromisoformat(item["data"]).date()
                return d >= cutoff
            except Exception:
                return True
        # se não tem data, mantemos
        return True

    filtrados = [c for c in concursos if _keep(c)]

    out = {
        "meses": months,
        "qtd": len(filtrados),
        "concursos": filtrados,
        "fonte": "sorteonline",
    }
    _cache_set(cache_key, out)
    return out


# --------------------------------------------------------------------
# Rotas
# --------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health ou /lotofacil?months=3",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    # Amostra rápida de reachability da ScraperAPI (sem travar health)
    return {"status": "ok", "message": "Lotofácil API online!"}


@app.get("/debug")
def debug(page: int = Query(1, ge=1)):
    """
    Mostra um pequeno diagnóstico do scraper (não retorna tudo para não pesar).
    """
    try:
        data = fetch_sorteonline_page(page=page)
        return JSONResponse(data)
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=502, detail=f"Falha HTTP ao acessar SorteOnline: {e}")
    except requests.ReadTimeout:
        raise HTTPException(
            status_code=504, detail="Timeout ao renderizar a página do SorteOnline (ScraperAPI).")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Erro de parsing/debug: {e}")


@app.get("/lotofacil")
def lotofacil(months: int = Query(3, ge=1)):
    """
    Retorna concursos dos últimos `months` meses (aceita >12 também).
    Observação: meses grandes podem demorar mais, pois exigem mais dados.
    """
    try:
        result = get_concursos_last_months(months)
        return JSONResponse(result)
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=502, detail=f"SorteOnline respondeu erro HTTP: {e}")
    except requests.ReadTimeout:
        raise HTTPException(
            status_code=504, detail="Timeout ao renderizar a página do SorteOnline (ScraperAPI).")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Erro processando resultados: {e}")


# --------------------------------------------------------------------
# Execução local (debug) - o Render usa o comando do render.yaml
# --------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(
        os.getenv("PORT", "8900")), reload=True)
