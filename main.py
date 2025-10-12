from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI(title="Lotofacil API", version="2.0")

# ==========================
# CONFIGURAÇÕES CORS (para permitir o app acessar)
# ==========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================
# CACHE LOCAL (em memória)
# ==========================
cache_data = {}
cache_time = {}

# ==========================
# ENDPOINT DE SAÚDE
# ==========================


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "Lotofacil API online!"}

# ==========================
# FUNÇÃO PRINCIPAL: BUSCAR RESULTADOS DO SITE
# ==========================


def get_results_from_site(months: int):
    """
    Busca os concursos da Lotofácil direto do site numeromania.com.br
    """
    base_url = "https://www.numeromania.com.br/estatisticas_lotofacil.asp"
    results = []
    end_date = datetime.today()
    start_date = end_date - timedelta(days=months * 30)

    for page in range(1, 50):  # percorre várias páginas se necessário
        url = f"{base_url}?pagina={page}"
        try:
            response = requests.get(url, timeout=15)
            if response.status_code != 200:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            rows = soup.select("table tr")

            for row in rows:
                cols = [c.text.strip() for c in row.find_all("td")]
                if len(cols) < 16:
                    continue
                try:
                    concurso = int(cols[0])
                    data_str = cols[1]
                    data = datetime.strptime(data_str, "%d/%m/%Y")
                    dezenas = [int(n) for n in cols[2:17]]

                    if start_date <= data <= end_date:
                        results.append({
                            "concurso": concurso,
                            "data": data_str,
                            "numeros": dezenas
                        })
                except Exception:
                    continue
        except Exception:
            break

    return results

# ==========================
# ENDPOINT PRINCIPAL
# ==========================


@app.get("/lotofacil")
def get_lotofacil(months: int = Query(3, ge=1, le=60)):
    """
    Retorna concursos da Lotofácil dos últimos X meses.
    Aceita até 60 meses, embora 1–12 seja mais rápido.
    """
    now = datetime.now()
    # cache válido por 6h
    if months in cache_data and (now - cache_time[months]).seconds < 6 * 3600:
        print(f"[CACHE] Servindo cache de {months}m")
        return {"meses": months, "concursos": cache_data[months]}

    print(f"[SITE] Atualizando dados para {months} meses...")
    results = get_results_from_site(months)
    cache_data[months] = results
    cache_time[months] = now
    return {"meses": months, "concursos": results, "qtd": len(results)}

# ==========================
# AGENDAMENTO AUTOMÁTICO
# ==========================


def update_cache():
    for m in [1, 2, 3, 4, 5, 6, 12]:
        data = get_results_from_site(m)
        cache_data[m] = data
        cache_time[m] = datetime.now()
        print(f"[CACHE] Atualizado para {m}m: {len(data)} concursos")


scheduler = BackgroundScheduler()
scheduler.add_job(update_cache, "interval", hours=6)
scheduler.start()

# ==========================
# EXECUÇÃO LOCAL
# ==========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8900, reload=True)
