"""
Monitor de Preços — Frutas (BA)
Petrolina-PE / Juazeiro-BA

Fonte : Preço da Hora BA (precodahora.ba.gov.br)
Destino: Supabase — schema cerebro, prefixo cfru_

Variáveis de ambiente necessárias:
  SUPABASE_URL          → URL do projeto Supabase
  SUPABASE_SERVICE_KEY  → service_role key (nunca a anon key)

Variáveis opcionais (modo cron por grupo):
  GRUPO_INICIO          → offset alfabético inicial (ex: 0, 15, 30)
  GRUPO_FIM             → offset alfabético final exclusivo (ex: 15, 30, 45)
  CRON_CONFIG_ID        → id da linha em cron_config para atualização de status

Modos de execução:
  Sem grupo        → filtra produtos por ativo=true (botão manual do admin)
  Com grupo        → ignora ativo, ordena alfabético e fatia [inicio:fim]
  Local            → pausa manual ao receber 429 persistente (beep + Enter)
  CI/CD            → pula produto bloqueado e continua (sem input manual)
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
    log.info("Modo CI detectado — pausas manuais desativadas")
else:
    log.info("Modo local detectado — pausas manuais ativadas")

if MODO_CRON:
    log.info("Modo CRON detectado — grupo [%d:%d] (ignora filtro ativo)", GRUPO_INICIO, GRUPO_FIM)
else:
    log.info("Modo MANUAL detectado — filtrando produtos por ativo=true")

# ── Configurações ─────────────────────────────────────────────

TZ = ZoneInfo("America/Recife")

REGIOES = [
    {"label": "BA-Juazeiro", "lat": -9.4167, "lon": -40.5028},
]

API_HOME       = "https://precodahora.ba.gov.br/"
API_BUSCA      = "https://precodahora.ba.gov.br/produtos/"
RAIO_KM        = 30
HORAS          = 720   # 30 dias — janela ampla para primeira coleta
SLEEP_REQUESTS = 15
SLEEP_PAGINAS  = 5
SLEEP_429      = 60
MAX_RETRIES    = 3
MAX_PAGINAS    = 20

# ── Extração de quantidade da embalagem ──────────────────────

def extrair_quantidade(nome_nf: str) -> float | None:
    nome_upper = nome_nf.upper()
    padrao = re.search(
        r'[\/\s\-]?(\d+(?:[.,]\d+)?)\s*(?:KG|KGS|K(?=\s|$)|LT|LTS|L(?=\s|$)|LI|ML|G(?=\s|$)|GR)\b',
        nome_upper
    )
    if padrao:
        return float(padrao.group(1).replace(',', '.'))
    if re.search(r'\bKG\b|\bKGS\b|\bLT\b|\bLTS\b', nome_upper):
        return 1.0
    return None


# ── Pausa manual por rate limit ───────────────────────────────

def pausa_manual(termo: str, regiao: str) -> bool:
    if EM_CI:
        log.warning("Rate limit persistente no CI — pulando produto | termo=%s regiao=%s", termo, regiao)
        return False

    log.warning("Rate limit persistente — pausa manual necessária | termo=%s regiao=%s", termo, regiao)
    print("\n" + "="*60)
    print("⚠  BLOQUEIO DE RATE LIMIT — AÇÃO NECESSÁRIA")
    print(f"   Produto: {termo} | Região: {regiao}")
    print()
    print("   1. Abra precodahora.ba.gov.br no browser")
    print("   2. Faça uma busca qualquer para resolver o captcha")
    print("   3. Volte aqui e pressione ENTER para retomar")
    print("="*60)
    try:
        sys.stdout.write('\a')
        sys.stdout.flush()
    except Exception:
        pass
    input("\nPressione ENTER após resolver o captcha no browser... ")
    print()
    return True


# ── Sessão HTTP com CSRF ──────────────────────────────────────

def criar_sessao() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    log.info("Obtendo sessão e CSRF...")
    resp = session.get(API_HOME, timeout=20)
    resp.raise_for_status()

    csrf = ""
    match = re.search(r'<meta id="validate" data-id="([^"]+)"', resp.text)
    if match:
        csrf = match.group(1)
        log.info("CSRF obtido | %s...", csrf[:20])
    else:
        log.warning("CSRF não encontrado")

    session.headers.update({
        "Accept":           "*/*",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":           "https://precodahora.ba.gov.br",
        "Referer":          "https://precodahora.ba.gov.br/produtos/",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken":      csrf,
    })

    return session


# ── Carregar dados do Supabase ────────────────────────────────

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


def carregar_lojas_existentes(sb: Client) -> set[str]:
    resp = sb.table("cfru_lojas").select("cnpj").execute()
    return {r["cnpj"] for r in (resp.data or [])}


# ── Filtros de qualidade ──────────────────────────────────────

def filtrar_item(item: dict) -> bool:
    prod  = item.get("produto", item)
    preco = float(prod.get("precoUnitario") or prod.get("precoLiquido") or 0)
    return preco > 0


# ── API do Preço da Hora BA ───────────────────────────────────

def _buscar_pagina(
    session: requests.Session,
    termo: str,
    lat: float,
    lon: float,
    regiao: str,
    pagina: int,
) -> list | None:
    data = {
        "termo":     termo,
        "horas":     HORAS,
        "latitude":  lat,
        "longitude": lon,
        "raio":      RAIO_KM,
        "pagina":    pagina,
        "ordenar":   "preco.asc",
    }

    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(API_BUSCA, data=data, timeout=20)

            if resp.status_code == 429:
                if tentativa == 1:
                    log.warning(
                        "Rate limit 429 | pág %d | tentativa %d/%d | aguardando %ds | termo=%s",
                        pagina, tentativa, MAX_RETRIES, SLEEP_429, termo,
                    )
                    time.sleep(SLEEP_429)
                elif tentativa == 2:
                    deve_renovar = pausa_manual(termo, regiao)
                    if deve_renovar:
                        return None
                    else:
                        return []
                continue

            resp.raise_for_status()

            if "text/html" in resp.headers.get("Content-Type", ""):
                log.warning("Sessão expirada | pág %d | termo=%s", pagina, termo)
                return None

            payload = resp.json()
            break

        except requests.RequestException as exc:
            log.warning("HTTP error | pág %d | tentativa %d/%d | termo=%s | %s",
                        pagina, tentativa, MAX_RETRIES, termo, exc)
            if tentativa < MAX_RETRIES:
                time.sleep(SLEEP_429)
            else:
                return []
        except ValueError:
            log.warning("JSON inválido | pág %d | termo=%s", pagina, termo)
            return []
    else:
        return []

    if payload.get("codigo") != 80 or not payload.get("resultado"):
        return []

    itens = payload["resultado"]
    return itens if isinstance(itens, list) else []


def buscar_produto(
    session: requests.Session,
    produto: dict,
    lat: float,
    lon: float,
    regiao: str,
) -> list[dict] | None:
    termo      = produto["busca"]
    resultados = []

    for pagina in range(1, MAX_PAGINAS + 1):
        if pagina > 1:
            time.sleep(SLEEP_PAGINAS)

        itens = _buscar_pagina(session, termo, lat, lon, regiao, pagina)

        if itens is None:
            return None

        if not itens:
            log.info("  Paginação concluída na pág %d | termo=%s", pagina, termo)
            break

        descartados = 0
        for item in itens:
            if not filtrar_item(item):
                descartados += 1
                continue

            prod  = item.get("produto", item)
            estab = item.get("estabelecimento", {})

            nome_loja = estab.get("nomeEstabelecimento") or ""
            cnpj_raw  = estab.get("cnpj")
            cnpj      = str(int(cnpj_raw)) if cnpj_raw else None

            preco                = float(prod.get("precoUnitario") or prod.get("precoLiquido") or 0)
            nome_nf              = prod.get("descricao") or termo
            quantidade_embalagem = extrair_quantidade(nome_nf)
            data_nfe             = prod.get("data") or None
            ncm_raw              = prod.get("ncm")
            ncm                  = int(ncm_raw) if ncm_raw else None
            ncm_grupo            = prod.get("ncmGrupo") or None
            gtin                 = prod.get("gtin") or None
            tipo_nfe_raw         = prod.get("tipoNFe")
            tipo_nfe             = int(tipo_nfe_raw) if tipo_nfe_raw else None
            uf                   = estab.get("uf") or None

            resultados.append({
                "regiao":               regiao,
                "municipio":            estab.get("municipio") or regiao,
                "fonte_api":            "precodahora_ba",
                "nome_nf":              nome_nf,
                "preco":                preco,
                "unidade":              (prod.get("unidade") or "UN").upper(),
                "loja":                 nome_loja or None,
                "cnpj":                 cnpj,
                "quantidade_embalagem": quantidade_embalagem,
                "data_nfe":             data_nfe,
                "ncm":                  ncm,
                "ncm_grupo":            ncm_grupo,
                "gtin":                 gtin,
                "tipo_nfe":             tipo_nfe,
                "uf":                   uf,
            })

        if descartados:
            log.info("  Pág %d: %d itens descartados pelos filtros", pagina, descartados)

        log.info("  Pág %d: %d resultados acumulados | termo=%s regiao=%s",
                 pagina, len(resultados), termo, regiao)

    return resultados


# ── Supabase ──────────────────────────────────────────────────

def conectar_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key:
        log.error("SUPABASE_URL ou SUPABASE_SERVICE_KEY não definidos.")
        sys.exit(1)

    return create_client(
        url, key,
        options=ClientOptions(schema="cerebro"),
    )


def abrir_coleta(sb: Client) -> int:
    fonte_label = "frutas_ba"
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


def inserir_precos(sb: Client, coleta_id: int, produto_id: int, registros: list[dict]) -> int:
    if not registros:
        return 0

    hoje_recife = datetime.now(TZ).date().isoformat()
    rows = [{"coleta_id": coleta_id, "produto_id": produto_id, "data_coleta": hoje_recife, **r}
            for r in registros]

    resp = (
        sb.table("cfru_precos")
        .upsert(rows, on_conflict="produto_id,regiao,data_coleta,cnpj,preco", ignore_duplicates=True)
        .execute()
    )
    return len(resp.data) if resp.data else 0


def registrar_lojas_novas(sb: Client, registros: list[dict], lojas_existentes: set[str]) -> set[str]:
    novos = 0
    for r in registros:
        cnpj = r.get("cnpj")
        if not cnpj or cnpj in lojas_existentes:
            continue
        try:
            sb.table("cfru_lojas").insert({
                "cnpj":          cnpj,
                "nome":          r.get("loja"),
                "municipio":     r.get("municipio"),
                "regiao":        r.get("regiao"),
                "uf":            r.get("uf") or ("BA" if "Juazeiro" in (r.get("regiao") or "") else "PE"),
                "ativo":         True,
                "bloqueado":     False,
                "dados_fixados": False,
            }).execute()
            lojas_existentes.add(cnpj)
            novos += 1
            log.info("  Nova loja registrada | CNPJ=%s nome=%s", cnpj, r.get("loja"))
        except Exception as e:
            log.warning("  Erro ao registrar loja CNPJ=%s | %s", cnpj, e)

    if novos:
        log.info("Novas lojas registradas: %d", novos)

    return lojas_existentes


# ── Execução principal ────────────────────────────────────────

def _com_retry(fn, tentativas=3, espera=15, descricao=""):
    """Repete fn() algumas vezes, absorvendo erros transitorios de rede
    (timeout do site-fonte, conexao HTTP/2 do Supabase encerrada, etc.)."""
    for tentativa in range(1, tentativas + 1):
        try:
            return fn()
        except Exception as exc:
            if tentativa >= tentativas:
                raise
            log.warning("Tentativa %d/%d falhou em '%s' (%s) - aguardando %ds e repetindo",
                        tentativa, tentativas, descricao, exc, espera)
            time.sleep(espera)


def main() -> None:
    log.info("=== Monitor de Frutas (BA) — iniciando ===")

    sb               = conectar_supabase()
    produtos         = _com_retry(lambda: carregar_produtos(sb), descricao="carregar_produtos")
    lojas_existentes = _com_retry(lambda: carregar_lojas_existentes(sb), descricao="carregar_lojas_existentes")

    if not produtos:
        log.warning("Nenhum produto para coletar nesse grupo/filtro.")
        atualizar_cron_config(sb, "sucesso", 0)
        sys.exit(0)

    session     = _com_retry(criar_sessao, descricao="criar_sessao")
    coleta_id   = abrir_coleta(sb)
    total_geral = 0
    erros       = []

    for produto in produtos:
        produto_id = produto["id"]
        termo      = produto["busca"]

        for regiao in REGIOES:
            label = regiao["label"]
            log.info("Buscando | produto_id=%d nome='%s' termo='%s' regiao=%s",
                     produto_id, produto["nome"], termo, label)

            try:
                registros = buscar_produto(session, produto, regiao["lat"], regiao["lon"], label)

                if registros is None:
                    if EM_CI:
                        log.warning("Sessão expirada no CI — renovando e tentando de novo")
                    else:
                        log.info("Renovando sessão após pausa...")
                    session   = criar_sessao()
                    registros = buscar_produto(session, produto, regiao["lat"], regiao["lon"], label) or []

                if registros:
                    lojas_existentes = registrar_lojas_novas(sb, registros, lojas_existentes)

                inseridos = inserir_precos(sb, coleta_id, produto_id, registros)
                total_geral += inseridos
                log.info("  → %d encontrados | %d inseridos", len(registros), inseridos)

            except Exception as exc:
                msg = f"produto_id={produto_id} regiao={label}: {exc}"
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
    try:
        main()
    except Exception:
        log.exception("Falha fatal na execucao - marcando cron_config como 'falha'")
        try:
            atualizar_cron_config(conectar_supabase(), "falha", 0)
        except Exception:
            pass
        sys.exit(1)
