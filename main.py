from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import re

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

# ============ APP ============
app = FastAPI(title="Lotofácil API", version="2.4")

# ============ CORS ============
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ ROTAS BÁSICAS ============


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

# ============ ENDPOINT DE DEBUG ============
# (ajuda a confirmar o HTML que o Render está recebendo)


@app.get("/debug")
def debug(page: int = 1, base: int = 0):
    url = BASE_URLS[base % len(BASE_URLS)] + f"?pagina={page}"
    r = _session().get(url, headers=HEADERS, timeout=25)
    soup = BeautifulSoup(r.content, "html.parser")
    return {
        "url": url,
        "status_code": r.status_code,
        "encoding": getattr(soup, "original_encoding", r.encoding),
        "snippet": soup.prettify()[:800],
        "headers_used": HEADERS,
    }


# ============ CACHE EM MEMÓRIA ============
_cache: Dict[int, List[Dict[str, Any]]] = {}
_cache_time: Dict[int, datetime] = {}
CACHE_TTL_SECONDS = 6 * 3600  # 6 horas

# ============ SCRAPING (Numeromania) ============
BASE_URLS = [
    "https://www.numeromania.com.br/estatisticas_lotofacil.asp",
    "https://www.numeromania.com.br/resultados_lotofacil.asp",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://www.numeromania.com.br/",
}

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
INT_RE = re.compile(r"\b(\d{1,2})\b")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _parse_row_relaxed(row: BeautifulSoup) -> Optional[Dict[str, Any]]:
    cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
    if not cols:
        return None

    row_text = " ".join(cols)

    # concurso
    concurso = None
    for c in re.findall(r"\b(\d{3,5})\b", row_text):
        try:
            concurso = int(c)
            break
        except Exception:
            pass
    if not concurso:
        try:
            c0 = int(cols[0])
            if c0 >= 100:
                concurso = c0
        except Exception:
            return None

    # data
    m = DATE_RE.search(row_text)
    if not m:
        return None
    data_str = m.group(1)

    # 15 dezenas (1..25)
    nums = []
    for m2 in INT_RE.finditer(row_text):
        n = int(m2.group(1))
        if 1 <= n <= 25:
            nums.append(n)
        if len(nums) >= 25:
            break
    if len(nums) < 15:
        return None
    dezenas = nums[:15]

    try:
        datetime.strptime(data_str, "%d/%m/%Y")
    except Exception:
        return None

    return {"concurso": concurso, "data": data_str, "numeros": dezenas}


def get_results_from_site(months: int) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=months * 30)

    sess = _session()

    for base_url in BASE_URLS:
        for page in range(1, 80):
            url = f"{base_url}?pagina={page}"
            try:
                r = sess.get(url, timeout=25)
                if r.status_code != 200:
                    break

                soup = BeautifulSoup(r.content, "html.parser")
                tables = soup.find_all("table")
                rows = []
                if tables:
                    for t in tables:
                        rows.extend(t.find_all("tr"))
                else:
                    rows = soup.find_all("tr")

                found_any = False
                for row in rows:
                    item = _parse_row_relaxed(row)
                    if not item:
                        continue
                    found_any = True

                    try:
                        d = datetime.strptime(item["data"], "%d/%m/%Y")
                    except Exception:
                        continue

                    if start_date <= d <= end_date:
                        results.append(item)

                if not found_any:
                    continue

                if results:
                    mais_antiga = min(
                        datetime.strptime(i["data"], "%d/%m/%Y") for i in results
                    )
                    if mais_antiga < start_date - timedelta(days=20):
                        break

            except Exception:
                break

        if results:
            break

    results.sort(key=lambda x: (datetime.strptime(
        x["data"], "%d/%m/%Y"), x["concurso"]))
    return results

# ============ ENDPOINT PRINCIPAL ============


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

# ============ AGENDADOR ============


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

# ============ EXECUÇÃO LOCAL ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, reload=True)
