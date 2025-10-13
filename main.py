from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import re

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

# ==== ADICIONE ESSE BLOCO NO main.py (perto dos imports/constantes) ====

import html
from fastapi import HTTPException

# headers mais “reais” de navegador
STRONG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.numeromania.com.br/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}


@app.get("/debug/numeromania")
def debug_numeromania(page: int = 1):
    """
    Só para diagnóstico: baixa a página do Numeromania e reporta status,
    tamanho e se achou tabela. NÃO usar em produção nem deixar público depois.
    """
    url = f"https://www.numeromania.com.br/estatisticas_lotofacil.asp?pagina={page}"
    try:
        resp = requests.get(url, headers=STRONG_HEADERS, timeout=20)
    except Exception as e:
        raise HTTPException(502, f"Erro de rede: {e}")

    text = resp.text or ""
    soup = BeautifulSoup(text, "html.parser")
    tables = soup.find_all("table")
    trs = soup.find_all("tr")

    # loga no Render
    print(
        f"[DEBUG] numeromania page={page} status={resp.status_code} "
        f"len={len(text)} tables={len(tables)} trs={len(trs)}"
    )

    # devolve um resumo (com início do HTML escapado)
    return {
        "url": url,
        "status": resp.status_code,
        "len": len(text),
        "tables": len(tables),
        "trs": len(trs),
        "snippet": html.escape(text[:1000]),
    }


app = FastAPI(title="Lotofácil API", version="2.3")

# -------- CORS --------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- Rotas básicas --------


@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health ou /lotofacil?months=3",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Lotofácil API online!"}


# -------- Cache em memória --------
_cache: Dict[int, List[Dict[str, Any]]] = {}
_cache_time: Dict[int, datetime] = {}
CACHE_TTL_SECONDS = 6 * 3600  # 6 horas

# -------- Scraping (Numeromania) --------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
}

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
INT_RE = re.compile(r"\b(\d{1,2})\b")


def _parse_row_relaxed(row: BeautifulSoup) -> Optional[Dict[str, Any]]:
    """
    Tenta extrair concurso, data e 15 dezenas de uma <tr>,
    mesmo que a quantidade de colunas varie.
    """
    cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
    if not cols:
        return None

    row_text = " ".join(cols)

    # concurso: primeiro inteiro com >= 3 dígitos é um bom indicativo
    concurso = None
    for c in re.findall(r"\b(\d{3,5})\b", row_text):
        try:
            concurso = int(c)
            break
        except Exception:
            pass
    if not concurso:
        # às vezes o primeiro <td> é o concurso
        try:
            c0 = int(cols[0])
            if c0 >= 100:
                concurso = c0
        except Exception:
            return None

    # data: padrão dd/mm/aaaa
    m = DATE_RE.search(row_text)
    if not m:
        return None
    data_str = m.group(1)

    # dezenas: pegue inteiros 1..25 no texto e filtre os 15 primeiros
    nums = []
    for m2 in INT_RE.finditer(row_text):
        n = int(m2.group(1))
        if 1 <= n <= 25:
            nums.append(n)
        if len(nums) >= 20:  # pega um pouco a mais, depois normaliza
            break

    # Heurística: muitas linhas têm os 15 números em sequência;
    # mantenha os primeiros 15.
    if len(nums) < 15:
        return None
    dezenas = nums[:15]

    # valida
    try:
        datetime.strptime(data_str, "%d/%m/%Y")
    except Exception:
        return None

    return {"concurso": concurso, "data": data_str, "numeros": dezenas}


def get_results_from_site(months: int) -> List[Dict[str, Any]]:
    """
    Busca concursos da Lotofácil direto do Numeromania.
    Tenta múltiplas páginas e faz parsing tolerante.
    """
    base_urls = [
        # páginas mais comuns do Numeromania (varia o nome, então tentamos ambas)
        "https://www.numeromania.com.br/estatisticas_lotofacil.asp",
        "https://www.numeromania.com.br/resultados_lotofacil.asp",
    ]

    results: List[Dict[str, Any]] = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=months * 30)

    for base_url in base_urls:
        for page in range(1, 80):
            url = f"{base_url}?pagina={page}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code != 200:
                    break

                soup = BeautifulSoup(r.text, "html.parser")

                # procure todas as tabelas e todas as linhas
                tables = soup.find_all("table")
                if not tables:
                    # fallback: tentar linhas gerais da página (pode não ser tabela)
                    rows = soup.find_all("tr")
                else:
                    rows = []
                    for t in tables:
                        rows.extend(t.find_all("tr"))

                found_any_on_page = False
                for row in rows:
                    item = _parse_row_relaxed(row)
                    if not item:
                        continue
                    found_any_on_page = True

                    # filtro por período
                    try:
                        d = datetime.strptime(item["data"], "%d/%m/%Y")
                    except Exception:
                        continue

                    if start_date <= d <= end_date:
                        results.append(item)

                # Se a página não teve nada aproveitável, segue para próxima base/loop
                if not found_any_on_page:
                    continue

                # atalho: se já passamos muito do corte, pare
                if results:
                    mais_antiga = min(
                        datetime.strptime(i["data"], "%d/%m/%Y") for i in results
                    )
                    if mais_antiga < start_date - timedelta(days=20):
                        break

            except Exception:
                break  # erro de rede/conteúdo: tenta próxima base ou encerra

        # Se já coletou algo com a 1ª base, nem precisa tentar a 2ª
        if results:
            break

    # normaliza (ordena)
    results.sort(key=lambda x: (datetime.strptime(
        x["data"], "%d/%m/%Y"), x["concurso"]))
    return results

# -------- Endpoint principal --------


@app.get("/lotofacil")
def get_lotofacil(months: int = Query(3, ge=1, le=60)):
    now = datetime.now()
    if (
        months in _cache
        and months in _cache_time
        and (now - _cache_time[months]).total_seconds() < CACHE_TTL_SECONDS
    ):
        return {"meses": months, "qtd": len(_cache[months]), "concursos": _cache[months]}

    results = get_results_from_site(months)
    _cache[months] = results
    _cache_time[months] = now
    return {"meses": months, "qtd": len(results), "concursos": results}

# -------- Agendador (somente periódico) --------


def update_cache():
    for m in [1, 2, 3, 4, 5, 6, 12]:
        try:
            data = get_results_from_site(m)
            _cache[m] = data
            _cache_time[m] = datetime.now()
            print(f"[CACHE] Atualizado para {m}m: {len(data)} concursos")
        except Exception as exc:
            print(f"[CACHE] Falha ao atualizar {m}m: {exc}")


scheduler = BackgroundScheduler()
scheduler.add_job(update_cache, "interval", hours=6)
scheduler.start()

# -------- Execução local --------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, reload=True)
