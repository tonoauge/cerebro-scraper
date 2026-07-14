"""
Monitor de Preços — Frutas PE (Menor Preço PR / Nota Paraná)
Petrolina-PE / Juazeiro-BA

Fonte : menorpreco.notaparana.pr.gov.br
Destino: Supabase — schema cerebro, prefixo cfru_
Tabelas: cfru_precos_pe, cfru_estabelecimentos
         cfru_coletas, cfru_produtos

Variáveis de ambiente necessárias:
  SUPABASE_URL          → URL do projeto Supabase
  SUPABASE_SERVICE_KEY  → service_role key

Variáveis opcionais (modo cron por grupo):
  GRUPO_INICIO          → offset alfabético inicial (ex: 0, 15, 30)
  GRUPO_FIM             → offset alfabético final exclusivo (ex: 15, 30, 45)
  CRON_CONFIG_ID        → id da linha em cron_config para atualização de status

Modos de execução:
  Sem grupo        → filtra produtos por ativo=true (botão manual do admin)
  Com grupo        → ignora ativo, ordena alfabético e fatia [inicio:fim]
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from supabase import create_client, Client, ClientOptions

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Modo de execução ──────────────────────────────────────────

EM_CI = os.environ.get('CI', 'false').lower() == 'true'

GRUPO_INICIO_RAW = os.environ.get('GRUPO_INICIO')
GRUPO_FIM_RAW    = os.environ.get('GRUPO_FIM')
CRON_CONFIG_ID   = os.environ.get('CRON_CONFIG_ID')

MODO_CRON = bool(GRUPO_INICIO_RAW) and bool(GRUPO_FIM_RAW)
GRUPO_INICIO = int(GRUPO_INICIO_RAW) if MODO_CRON else None
GRUPO_FIM    = int(GRUPO_FIM_RAW)    if MODO_CRON else None

if EM_CI:
    log.info("Modo CI detectado")
else:
    log.info("Modo local detectado")

if MODO_CRON:
    log.info("Modo CRON detectado — grupo [%d:%d] (ignora filtro ativo)", GRUPO_INICIO, GRUPO_FIM)
else:
    log.info("Modo MANUAL detectado — filtrando produtos por ativo=true")

# ── Configurações ─────────────────────────────────────────────

TZ             = ZoneInfo("America/Recife")
# Para descobrir o código de uma nova região: abra o app Nota Paraná, ative um proxy
# (ex: mitmproxy/Charles) e pesquise a cidade desejada — capture o param "local" na
# chamada GET /api/v1/categorias ou /api/v1/produtos.
LOCAIS         = [
    "7n75qzfef",      # ponto 1
    "7n754bpeh",      # ponto 2
    "7n7kguwzs",      # ponto 3
]
RAIO           = 20              # km
DATA_DIAS      = 5               # dias retroativos
ORDENS         = [1]             # API ignora parâmetro ordem; mantém [1] por compatibilidade
MAX_RESULTADOS = 200             # máx por produto (API não tem paginação)
SLEEP_REQUESTS = 15              # pausa entre produtos (segundos)
SLEEP_429      = 60              # pausa em caso de rate limit
MAX_RETRIES    = 3

BASE_URL = "https://menorpreco.notaparana.pr.gov.br/api/v1"

HEADERS = {
    "Accept-Charset":  "UTF-8",
    "Accept-Encoding": "gzip",
    "Connection":      "Keep-Alive",
    "Content-Type":    "application/json",
    "User-Agent":      "Dalvik/2.1.0 (Linux; U; Android 16; 2412DPC0AG Build/BP2A.250605.031.A3)",
}

# ── Cache local de categorias PR (evita chamadas repetidas no mesmo run) ──
_cache_categorias: dict[str, int | None] = {}

# ── Extração de quantidade da embalagem ──────────────────────

def extrair_quantidade(desc: str) -> tuple[float | None, str | None]:
    desc_upper = desc.upper()
    padrao = re.search(
        r'(\d+(?:[.,]\d+)?)\s*(KGS|KG|K(?=\s|$)|LTS|LT|L(?=\s|$)|GRS|GR|ML|G(?=\s|$))',
        desc_upper,
    )
    if padrao:
        valor = padrao.group(1).replace(",", ".")
        return float(valor), padrao.group(2)
    if re.search(r'\bKG\b|\bKGS\b|\bLT\b|\bLTS\b', desc_upper):
        return 1.0, None
    return None, None


def calcular_preco_por_kg(preco: float, qtd: float | None, unidade: str) -> float | None:
    if not qtd or qtd <= 0:
        return None
    unidade = (unidade or "").upper()
    if unidade in ("KG", "KGS", "K", "L", "LT", "LTS"):
        return round(preco / qtd, 4)
    if unidade == "ML":
        return round(preco / (qtd / 1000), 4)
    if unidade in ("G", "GR", "GRS"):
        return round(preco / (qtd / 1000), 4)
    return None


# ── API Menor Preço PR ────────────────────────────────────────

def buscar_categoria_pr(session: requests.Session, termo: str, local: str) -> int | None:
    chave = (local, termo)
    if chave in _cache_categorias:
        return _cache_categorias[chave]

    url = f"{BASE_URL}/categorias"
    params = {"termo": termo, "local": local, "raio": RAIO, "data": DATA_DIAS}
    try:
        r = session.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        dados = r.json()
        categorias = dados.get("categorias") or dados.get("resultado") or []
        if categorias and isinstance(categorias[0], dict):
            cat_id = categorias[0].get("id") or categorias[0].get("codigo")
            _cache_categorias[chave] = int(cat_id) if cat_id else None
            log.info("  Categoria PR descoberta | termo='%s' categoria_id=%s", termo, cat_id)
            return _cache_categorias[chave]
    except Exception as exc:
        log.warning("  Erro ao buscar categoria PR | termo='%s' | %s", termo, exc)

    _cache_categorias[chave] = None
    return None


def buscar_precos_pr(
    session: requests.Session,
    produto: dict,
    ordem: int,
    local: str,
) -> list[dict]:
    termo  = produto["busca"]
    cat_id = buscar_categoria_pr(session, termo, local)

    params = {
        "termo": termo,
        "local": local,
        "raio":  RAIO,
        "data":  DATA_DIAS,
        "ordem": ordem,
    }
    if cat_id:
        params["categoria"] = cat_id

    url   = f"{BASE_URL}/produtos"
    dados = None

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=HEADERS, params=params, timeout=20)

            if r.status_code == 429:
                log.warning("Rate limit 429 | tentativa %d/%d | termo='%s' | aguardando %ds",
                            tentativa, MAX_RETRIES, termo, SLEEP_429)
                if tentativa < MAX_RETRIES:
                    time.sleep(SLEEP_429)
                    continue
                else:
                    log.warning("Rate limit persistente — pulando produto | termo='%s'", termo)
                    return []

            r.raise_for_status()
            dados = r.json()
            break

        except requests.RequestException as exc:
            log.warning("HTTP error | tentativa %d/%d | termo='%s' | %s",
                        tentativa, MAX_RETRIES, termo, exc)
            if tentativa < MAX_RETRIES:
                time.sleep(SLEEP_429)
            else:
                return []
        except ValueError:
            log.warning("JSON inválido | termo='%s'", termo)
            return []

    if dados is None:
        log.warning("Payload não obtido após retries | termo='%s'", termo)
        return []

    itens = dados.get("produtos") or []
    total = dados.get("total", 0)
    pmin  = dados.get("precos", {}).get("min")
    pmax  = dados.get("precos", {}).get("max")
    log.info("  → %d resultados | min R$%s | max R$%s", total, pmin, pmax)

    resultados = []
    for item in itens[:MAX_RESULTADOS]:
        estab = item.get("estabelecimento", {})
        preco = float(item.get("valor") or 0)
        if preco <= 0:
            continue

        desc  = item.get("desc") or termo
        qtd, unidade_qtd = extrair_quantidade(desc)

        unidade = unidade_qtd or "UN"
        if not unidade_qtd:
            m = re.search(r'\b(KG|KGS|LT|LTS|ML|GRS|GR?)\b', desc.upper())
            if m:
                unidade = m.group(1)

        preco_kg = calcular_preco_por_kg(preco, qtd, unidade)

        resultados.append({
            "nome_nf":              desc,
            "ncm":                  item.get("ncm") or None,
            "gtin":                 item.get("gtin") or None,
            "preco":                preco,
            "unidade":              unidade,
            "quantidade_embalagem": qtd,
            "preco_por_kg":         preco_kg,
            "data_nfe":             item.get("datahora") or None,
            "nrdoc":                item.get("nrdoc") or None,
            "local_item":           item.get("local") or None,
            "distkm":               float(item.get("distkm") or 0) or None,
            "codigo_estab":         estab.get("codigo"),
            "loja":                 estab.get("nm_fan") or estab.get("nm_emp") or None,
            "nm_emp":               estab.get("nm_emp") or None,
            "logradouro":           f"{estab.get('tp_logr','')} {estab.get('nm_logr','')} {estab.get('nr_logr','')}".strip() or None,
            "bairro":               estab.get("bairro") or None,
            "mun":                  estab.get("mun") or None,
            "uf":                   estab.get("uf") or None,
            "municipio":            estab.get("mun") or None,
            "regiao":               "PE-Petrolina",
            "fonte_api":            "menorpreco_pr",
        })

    return resultados


# ── Supabase ──────────────────────────────────────────────────

def conectar_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        log.error("SUPABASE_URL ou SUPABASE_SERVICE_KEY não definidos.")
        sys.exit(1)
    return create_client(url, key, options=ClientOptions(schema="cerebro"))


def carregar_produtos(sb: Client) -> list[dict]:
    if MODO_CRON:
        resp = (sb.table("cfru_produtos")
                  .select("id, nome, busca, categoria")
                  .order("nome", desc=False)
                  .execute())
        todos = resp.data or []
        produtos = todos[GRUPO_INICIO:GRUPO_FIM]
        log.info("Modo CRON: %d produtos no grupo [%d:%d] de %d totais",
                 len(produtos), GRUPO_INICIO, GRUPO_FIM, len(todos))
    else:
        resp = (sb.table("cfru_produtos")
                  .select("id, nome, busca, categoria")
                  .eq("ativo", True)
                  .order("nome", desc=False)
                  .execute())
        produtos = resp.data or []
        log.info("Modo MANUAL: %d produtos ativos carregados", len(produtos))

    return produtos


def carregar_estabelecimentos_existentes(sb: Client) -> set[str]:
    resp = sb.table("cfru_estabelecimentos").select("codigo").execute()
    return {r["codigo"] for r in (resp.data or [])}


def abrir_coleta(sb: Client) -> int:
    fonte_label = "frutas_pe"
    if MODO_CRON:
        fonte_label += f"_grupo_{GRUPO_INICIO}-{GRUPO_FIM}"
    resp = (
        sb.table("cfru_coletas")
        .insert({"status": "em_andamento", "fonte": fonte_label})
        .execute()
    )
    coleta_id = resp.data[0]["id"]
    log.info("Coleta iniciada | id=%d", coleta_id)
    return coleta_id


def fechar_coleta(sb: Client, coleta_id: int, total: int, erros: list) -> None:
    status = "sucesso" if not erros else ("erro_parcial" if total > 0 else "falha")
    sb.table("cfru_coletas").update({
        "finalizado_em":   datetime.now(TZ).isoformat(),
        "status":          status,
        "total_registros": total,
        "erros":           json.dumps(erros, ensure_ascii=False) if erros else None,
    }).eq("id", coleta_id).execute()
    log.info("Coleta finalizada | id=%d status=%s total=%d erros=%d",
             coleta_id, status, total, len(erros))


def atualizar_cron_config(sb: Client, status: str, total: int) -> None:
    if not CRON_CONFIG_ID:
        return
    try:
        sb.table("cron_config").update({
            "ultima_execucao": datetime.now(TZ).isoformat(),
            "ultimo_status":   status,
            "ultimo_total":    total,
        }).eq("id", int(CRON_CONFIG_ID)).execute()
        log.info("cron_config atualizado | id=%s status=%s total=%d", CRON_CONFIG_ID, status, total)
    except Exception as exc:
        log.warning("Falha ao atualizar cron_config: %s", exc)


def registrar_estabelecimentos_novos(
    sb: Client,
    registros: list[dict],
    existentes: set[str],
) -> set[str]:
    novos = 0
    for r in registros:
        codigo = r.get("codigo_estab")
        if not codigo or codigo in existentes:
            continue
        try:
            sb.table("cfru_estabelecimentos").insert({
                "codigo":     codigo,
                "nm_fan":     r.get("loja"),
                "nm_emp":     r.get("nm_emp"),
                "logradouro": r.get("logradouro"),
                "bairro":     r.get("bairro"),
                "mun":        r.get("mun"),
                "uf":         r.get("uf"),
            }).execute()
            existentes.add(codigo)
            novos += 1
            log.info("  Novo estabelecimento | codigo=%.20s... nome=%s", codigo, r.get("loja"))
        except Exception as exc:
            log.warning("  Erro ao registrar estabelecimento | %s", exc)

    if novos:
        log.info("Novos estabelecimentos registrados: %d", novos)
    return existentes


def inserir_precos(
    sb: Client,
    coleta_id: int,
    produto_id: int,
    registros: list[dict],
) -> int:
    if not registros:
        return 0

    hoje = datetime.now(TZ).date().isoformat()
    rows = []
    for r in registros:
        rows.append({
            "coleta_id":            coleta_id,
            "produto_id":           produto_id,
            "data_coleta":          hoje,
            "data_nfe":             r.get("data_nfe"),
            "nome_nf":              r.get("nome_nf"),
            "ncm":                  r.get("ncm"),
            "gtin":                 r.get("gtin"),
            "preco":                r.get("preco"),
            "unidade":              r.get("unidade"),
            "quantidade_embalagem": r.get("quantidade_embalagem"),
            "preco_por_kg":         r.get("preco_por_kg"),
            "codigo_estab":         r.get("codigo_estab"),
            "loja":                 r.get("loja"),
            "municipio":            r.get("municipio"),
            "uf":                   r.get("uf"),
            "regiao":               r.get("regiao"),
            "distkm":               r.get("distkm"),
            "local_item":           r.get("local_item"),
            "nrdoc":                r.get("nrdoc"),
            "fonte_api":            "menorpreco_pr",
        })

    resp = (
        sb.table("cfru_precos_pe")
        .upsert(rows, on_conflict="produto_id,regiao,data_coleta,codigo_estab,preco,nrdoc", ignore_duplicates=True)
        .execute()
    )
    return len(resp.data) if resp.data else 0


# ── Main ──────────────────────────────────────────────────────

def main() -> None:
    log.info("=== Monitor de Frutas PE — iniciando ===")
    log.info("    Locais: %s | Raio: %dkm | Período: %dd | Ordens: %s", LOCAIS, RAIO, DATA_DIAS, ORDENS)

    sb         = conectar_supabase()
    produtos   = carregar_produtos(sb)
    existentes = carregar_estabelecimentos_existentes(sb)

    if not produtos:
        log.warning("Nenhum produto para coletar nesse grupo/filtro.")
        atualizar_cron_config(sb, "sucesso", 0)
        sys.exit(0)

    session     = requests.Session()
    coleta_id   = abrir_coleta(sb)
    total_geral = 0
    erros       = []

    for produto in produtos:
        produto_id = produto["id"]
        termo      = produto["busca"]
        nome       = produto["nome"]

        log.info("Buscando | id=%d nome='%s' termo='%s'", produto_id, nome, termo)

        try:
            todos_registros: list[dict] = []
            vistos: set[tuple] = set()

            for local in LOCAIS:
                for i, ordem in enumerate(ORDENS):
                    log.info("  Local %s | Ordem %d", local, ordem)
                    regs = buscar_precos_pr(session, produto, ordem, local)
                    for r in regs:
                        chave = (r.get("codigo_estab"), r.get("preco"), r.get("nrdoc"))
                        if chave not in vistos:
                            vistos.add(chave)
                            todos_registros.append(r)
                    time.sleep(SLEEP_REQUESTS)

            if todos_registros:
                existentes = registrar_estabelecimentos_novos(sb, todos_registros, existentes)

            inseridos = inserir_precos(sb, coleta_id, produto_id, todos_registros)
            total_geral += inseridos
            log.info("  → %d coletados únicos | %d inseridos", len(todos_registros), inseridos)

        except Exception as exc:
            msg = f"produto_id={produto_id} nome={nome}: {exc}"
            log.error("ERRO | %s", msg)
            erros.append(msg)

        time.sleep(SLEEP_REQUESTS)

    fechar_coleta(sb, coleta_id, total_geral, erros)

    status_final = "sucesso" if not erros else ("erro_parcial" if total_geral > 0 else "falha")
    atualizar_cron_config(sb, status_final, total_geral)

    log.info("=== Concluído | %d registros novos ===", total_geral)

    if total_geral == 0 and erros:
        sys.exit(1)


if __name__ == "__main__":
    main()
