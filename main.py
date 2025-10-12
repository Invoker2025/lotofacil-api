from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from apscheduler.schedulers.background import BackgroundScheduler


app = FastAPI(title="Lotofácil API", version="2.1")

# -----------------------------
# CORS (permite o app iOS/Expo)
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Rotas básicas
# -----------------------------


@app.get("/")
def root():
    # Resposta amigável na raiz (evita 404 nos logs)
    return {
        "ok": True,
        "message": "Lotofácil API online. Use /health ou /lotofacil?months=3",
        "docs": "/docs",
        "redoc": "/redoc",
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Lotofácil API online!"}


# -----------------------------
# Cache em memória
# -----------------------------
_cache: Dict[int, List[Dict[str, Any]]] = {}
_cache_time: Dict[int, datetime] = {}
CACHE_TTL_SECONDS = 6 * 3600  # 6h


# -----------------------------
# Coletor (scraping Numeromania)
# -----------------------------
HEADERS = {
    # User-Agent para evitar bloqueio do site
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
}


def get_results_from_site(months: int) -> List[Dict[str, Any]]:
    """
    Busca concursos da Lotofácil direto do Numeromania.
    Coleta várias páginas até cobrir o intervalo desejado.
    """
    base_url = "https://www.numeromania.com.br/estatisticas_lotofacil.asp"
    results: List[Dict[str, Any]] = []

    end_date = datetime.today()
    start_date = end_date - timedelta(days=months * 30)

    # Varre páginas enquanto achar conteúdo
    # (ajuste o range se precisar de mais histórico)
    for page in range(1, 80):
        url = f"{base_url}?pagina={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                break

            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("table tr")
            if not rows:
                # Em caso de mudança no site, evita loop infinito
                break

            encontrou_algum = False
            for row in rows:
                cols = [c.get_text(strip=True) for c in row.find_all("td")]
                # Esperado: [concurso, data, 15 dezenas, ...]
                if len(cols) < 17:
                    continue
                try:
                    concurso = int(cols[0])
                    data_str = cols[1]
                    data = datetime.strptime(data_str, "%d/%m/%Y")
                    dezenas = [int(n) for n in cols[2:17]]

                    encontrou_algum = True
                    if start_date <= data <= end_date:
                        results.append(
                            {"concurso": concurso, "data": data_str,
                                "numeros": dezenas}
                        )
                    # Se já passamos do período, podemos continuar só até achar todos
                except Exception:
                    # Linha de cabeçalho ou formato inesperado
                    continue

            # Se a página não tinha linhas úteis, paramos
            if not encontrou_algum:
                break

            # Pequena curta-circuito: se já coletamos muitos concursos
            # e a data mais antiga ultrapassa o recorte, podemos parar.
            if results and len(results) > 0:
                mais_antiga = min(
                    datetime.strptime(item["data"], "%d/%m/%Y") for item in results
                )
                if mais_antiga < start_date - timedelta(days=15):
                    break

        except Exception:
            # Qualquer erro de rede/conteúdo encerra a coleta
            break

    # Ordena por data/concurso se quiser consistência
    results.sort(key=lambda x: (datetime.strptime(
        x["data"], "%d/%m/%Y"), x["concurso"]))
    return results


# -----------------------------
# Endpoint principal
# -----------------------------
@app.get("/lotofacil")
def get_lotofacil(months: int = Query(3, ge=1, le=60)):
    """
    Retorna concursos da Lotofácil dos últimos X meses (1..60).
    Estrutura:
      {
        "meses": X,
        "qtd": N,
        "concursos": [{ "concurso": 1234, "data": "dd/mm/aaaa", "numeros": [..15..] }]
      }
    """
    now = datetime.now()

    # Cache válido?
    if (
        months in _cache
        and months in _cache_time
        and (now - _cache_time[months]).total_seconds() < CACHE_TTL_SECONDS
    ):
        return {"meses": months, "qtd": len(_cache[months]), "concursos": _cache[months]}

    # Atualiza do site
    results = get_results_from_site(months)
    _cache[months] = results
    _cache_time[months] = now
    return {"meses": months, "qtd": len(results), "concursos": results}


# -----------------------------
# Agendador (atualiza cache)
# -----------------------------
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
# Atualiza a cada 6h
scheduler.add_job(update_cache, "interval", hours=6)
# (Opcional) Primeira atualização logo após iniciar
scheduler.add_job(update_cache, "date", run_date=datetime.now())
scheduler.start()


# -----------------------------
# Execução local
# -----------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8900, reload=True)
