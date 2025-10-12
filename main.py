# main.py
# -------------------------------------------
# Lotofácil API (FastAPI) – sem CSV
# - Busca direta na API oficial da CAIXA (portaldeloterias)
# - Pré-cache 1..12 meses
# - Aceita qualquer months >= 1 (limite seguro configurável)
# - Frequências/combinação segundo a sua regra
# - Scheduler atualiza tudo que já estiver no cache
# -------------------------------------------

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from apscheduler.schedulers.background import BackgroundScheduler

from datetime import datetime
from dateutil.relativedelta import relativedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# -------------- Config ---------------

CAIXA_BASE = "https://servicebus2.caixa.gov.br/portaldeloterias/api/lotofacil"
TIMEOUT = 15
# pré-carregar (rápido)
CACHE_MONTH_CHOICES = list(range(1, 13))  # 1..12
# aceitar on-demand até este teto (15, 20, 30... até 36 por padrão)
SAFE_MAX_MONTHS = 36


# -------------- App + CORS ---------------

app = FastAPI(title="Lotofácil API", version="2.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # em produção: ["https://SEU-DOMINIO.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------- Modelos ---------------

class Frequencia(BaseModel):
    dezena: int
    freq: int


class LotofacilResponse(BaseModel):
    periodo: str
    final_15: List[int]
    top12: List[int]
    bottom3: List[int]
    frequencias: List[Frequencia]
    total_concursos_periodo: int
    total_concursos_total: int
    source: Optional[str] = None
    coverage: Optional[Dict[str, Any]] = None
    last_updated: Optional[str] = None


# -------------- Sessão HTTP com retry ---------------

def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    # headers para evitar 403 da CAIXA
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://loterias.caixa.gov.br/",
    })
    return s


SESSION = make_session()


# -------------- Utilitários ---------------

def parse_data_apuracao(s: str) -> datetime:
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.utcnow()


def asc(nums: List[int]) -> List[int]:
    return sorted(nums)


def is_even(n: int) -> bool:
    return n % 2 == 0


def is_odd(n: int) -> bool:
    return n % 2 == 1


# -------------- Estratégia pedida ---------------

def build_6even6odd_top__2even1odd_bottom(freqs: List[Frequencia]) -> List[int]:
    top_sorted = sorted(freqs, key=lambda f: (-f.freq, f.dezena))
    bottom_sorted = sorted(freqs, key=lambda f: (f.freq, f.dezena))

    top_pool = [f.dezena for f in top_sorted]
    bottom_pool = [f.dezena for f in bottom_sorted]

    chosen = set()
    result: List[int] = []

    # top: 6 pares + 6 ímpares
    for n in top_pool:
        if len([x for x in result if is_even(x)]) >= 6:
            break
        if n not in chosen and is_even(n):
            chosen.add(n)
            result.append(n)
    for n in top_pool:
        if len([x for x in result if is_odd(x)]) >= 6:
            break
        if n not in chosen and is_odd(n):
            chosen.add(n)
            result.append(n)

    # bottom: 2 pares + 1 ímpar
    for n in bottom_pool:
        if len([x for x in result if is_even(x)]) >= 8:
            break  # 6 (top) + 2
        if n not in chosen and is_even(n):
            chosen.add(n)
            result.append(n)
    for n in bottom_pool:
        if len([x for x in result if is_odd(x)]) >= 7:
            break   # 6 (top) + 1
        if n not in chosen and is_odd(n):
            chosen.add(n)
            result.append(n)

    # completa até 15 com top
    for n in top_pool:
        if len(result) >= 15:
            break
        if n not in chosen:
            chosen.add(n)
            result.append(n)

    return asc(result[:15])


# -------------- Coleta da CAIXA ---------------

def caixa_get_latest() -> Dict[str, Any]:
    r = SESSION.get(CAIXA_BASE, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def caixa_get_concurso(numero: int) -> Dict[str, Any]:
    r = SESSION.get(f"{CAIXA_BASE}/{numero}", timeout=TIMEOUT)
    if r.status_code == 404:
        r = SESSION.get(f"{CAIXA_BASE}?concurso={numero}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def coletar_periodo(months: int) -> List[Dict[str, Any]]:
    """
    Parte do último concurso e retrocede até cruzar a data limite (now - months).
    """
    latest = caixa_get_latest()
    lista = []

    def extrair(obj: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "numero": int(obj.get("numero")),
            "dataApuracao": obj.get("dataApuracao"),
            "listaDezenas": [int(x) for x in obj.get("listaDezenas", [])],
        }

    first = extrair(latest)
    lista.append(first)

    limite = datetime.utcnow() - relativedelta(months=months)
    atual = first["numero"] - 1

    while atual > 0:
        try:
            cur = extrair(caixa_get_concurso(atual))
        except Exception:
            break
        dt = parse_data_apuracao(cur["dataApuracao"])
        if dt < limite:
            break
        lista.append(cur)
        atual -= 1

    lista.sort(key=lambda d: parse_data_apuracao(d["dataApuracao"]))
    return lista


# -------------- Frequências ---------------

def calcular_frequencias(draws: List[Dict[str, Any]]) -> List[Frequencia]:
    counts = {i: 0 for i in range(1, 26)}
    for d in draws:
        for n in d["listaDezenas"]:
            if 1 <= n <= 25:
                counts[n] += 1
    return [Frequencia(dezena=i, freq=counts[i]) for i in range(1, 26)]


# -------------- Cache ---------------

class CacheEntry(BaseModel):
    response: LotofacilResponse
    last_updated: str


CACHE: Dict[int, CacheEntry] = {}  # chave: months


def montar_resposta(months: int) -> LotofacilResponse:
    draws = coletar_periodo(months)
    total_periodo = len(draws)

    freqs = calcular_frequencias(draws)
    top12 = [f.dezena for f in sorted(
        freqs, key=lambda f: (-f.freq, f.dezena))[:12]]
    bottom3 = [f.dezena for f in sorted(
        freqs, key=lambda f: (f.freq, f.dezena))[:3]]
    final_15 = build_6even6odd_top__2even1odd_bottom(freqs)

    periodo_txt = f"Últimos {months} " + ("mês" if months == 1 else "meses")
    resp = LotofacilResponse(
        periodo=periodo_txt,
        final_15=final_15,
        top12=top12,
        bottom3=bottom3,
        frequencias=freqs,
        total_concursos_periodo=total_periodo,
        total_concursos_total=0,  # opcional: buscar total histórico depois
        source="caixa:portaldeloterias",
        coverage={"months": months},
        last_updated=datetime.utcnow().isoformat(),
    )
    return resp


def atualizar_cache_for(months: int):
    try:
        resp = montar_resposta(months)
        CACHE[months] = CacheEntry(
            response=resp, last_updated=resp.last_updated)
        print(
            f"[CACHE] Atualizado para {months}m: {resp.total_concursos_periodo} concursos")
    except Exception as e:
        print(f"[CACHE] Falha ao atualizar {months}m:", e)


# -------------- Scheduler ---------------

scheduler = BackgroundScheduler()


@app.on_event("startup")
def on_startup():
    # pré-carrega 1..12 (rápido)
    for m in CACHE_MONTH_CHOICES:
        atualizar_cache_for(m)

    # Atualiza periodicamente TUDO que já está no cache (inclui 15/20/30 após 1ª chamada)
    scheduler.add_job(
        lambda: [atualizar_cache_for(m) for m in list(CACHE.keys())],
        "interval",
        minutes=15,
        id="lotofacil_update",
        replace_existing=True
    )
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()


# -------------- Endpoints ---------------

@app.get("/health")
def health():
    return {"status": "ok", "cache": {m: c.last_updated for m, c in CACHE.items()}}


@app.get("/lotofacil", response_model=LotofacilResponse)
def lotofacil(months: int = Query(3, ge=1)):
    # aplica teto de segurança para proteger a CAIXA/servidor
    if months > SAFE_MAX_MONTHS:
        months = SAFE_MAX_MONTHS

    # gera on-demand se não existir (1ª vez pode demorar)
    if months not in CACHE:
        atualizar_cache_for(months)

    return CACHE[months].response
