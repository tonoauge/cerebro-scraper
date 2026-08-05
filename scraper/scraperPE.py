"""
Monitor de Preços — Menor Preço PR (dados PE/BA via Nota Paraná)
Petrolina-PE / Juazeiro-BA

Fonte : menorpreco.notaparana.pr.gov.br
Destino: Supabase — schema cerebro
Tabelas: ADSefPR_precos, ADSefPR_estabelecimentos
         ADSef_coletas (reaproveitada), ADSef_produtos (reaproveitada)

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from supabase import create_client, Client, ClientOptions

# ── Logging ──────────────────────────────────────────────────

# O console do Windows usa cp1252 e nao imprime as setas e acentos do log: cada linha
# vira um traceback de UnicodeEncodeError e o log fica ilegivel. Forcar UTF-8 resolve.
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# O cliente do Supabase loga uma linha por chamada HTTP, o que domina o log e o torna
# ilegivel num run longo. Erros de rede continuam aparecendo (nivel WARNING).
for _ruido in ("httpx", "httpcore", "hpack", "urllib3"):
    logging.getLogger(_ruido).setLevel(logging.WARNING)

# ── Modo de execução ──────────────────────────────────────────

EM_CI = os.environ.get('CI', 'false').lower() == 'true'

GRUPO_INICIO_RAW = os.environ.get('GRUPO_INICIO')
GRUPO_FIM_RAW    = os.environ.get('GRUPO_FIM')
CRON_CONFIG_ID   = os.environ.get('CRON_CONFIG_ID')

# Modo cron: ambos precisam estar PREENCHIDOS (não-None E não-vazios).
# O GitHub Actions passa "" quando inputs opcionais não são preenchidos.
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
LOCAL          = "7n74tuuvek0"   # código da região Petrolina/Juazeiro
RAIO           = 20              # km
DATA_DIAS      = 3               # dias retroativos
ORDEM          = 2               # 2 = mais recente primeiro
MAX_RESULTADOS = 200             # máx por produto (API não tem paginação)
# Pausa entre produtos. Ajustavel por env para o run local poder ir mais devagar sem
# quebrar os lotes do Actions: la sao 30 produtos por job com timeout de 30 min, entao
# 60s ja estouraria. Local (modo MANUAL, sem timeout) pode usar SLEEP_REQUESTS=60.
SLEEP_REQUESTS = int(os.environ.get("SLEEP_REQUESTS", "30"))   # 15s ate 04/08/2026
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


# ── Detecção de resposta envenenada ───────────────────────────────────────
# Desde 2026-07-23 a API devolve dados FABRICADOS (municípios e lojas com letras
# trocadas, UFs sorteadas em terços, preços aleatórios) para as chamadas vindas
# dos IPs do GitHub Actions. Do mesmo endpoint, com os mesmos parâmetros, um IP
# residencial recebe dado real — o corte é por IP.
#
# Assinatura infalível: no dado real, `local` é o geohash do ESTABELECIMENTO,
# distinto a cada loja. No dado fabricado, a API ecoa de volta o geohash que
# ENVIAMOS na consulta (LOCAL). Igualdade entre os dois não acontece por acaso.

class RespostaEnvenenada(Exception):
    """A API devolveu dados sintéticos — abortar sem gravar nada."""


def detectar_envenenamento(itens: list[dict], local: str) -> None:
    """Levanta RespostaEnvenenada se a maioria dos itens ecoar o geohash consultado.

    O limiar de metade evita que uma coincidência isolada derrube um run legítimo.
    """
    if not itens:
        return
    ecoados = sum(1 for it in itens if (it.get("local") or "") == local)
    if ecoados / len(itens) >= 0.5:
        raise RespostaEnvenenada(
            f"local='{local}' ecoado em {ecoados}/{len(itens)} itens"
        )

# ── Extração de quantidade da embalagem ──────────────────────

def extrair_quantidade(desc: str) -> tuple[float | None, str | None]:
    """Retorna (qtd, unidade_detectada). A unidade vem do mesmo match — útil
    quando a descrição tem "500GR" colado, sem fronteira de palavra antes."""
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

def buscar_categoria_pr(session: requests.Session, termo: str) -> int | None:
    """Descobre o ID numérico de categoria da API do PR para o termo."""
    if termo in _cache_categorias:
        return _cache_categorias[termo]

    url = f"{BASE_URL}/categorias"
    params = {"termo": termo, "local": LOCAL, "raio": RAIO, "data": DATA_DIAS}
    try:
        r = session.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        dados = r.json()
        categorias = dados.get("categorias") or dados.get("resultado") or []
        if categorias and isinstance(categorias[0], dict):
            cat_id = categorias[0].get("id") or categorias[0].get("codigo")
            _cache_categorias[termo] = int(cat_id) if cat_id else None
            log.info("  Categoria PR descoberta | termo='%s' categoria_id=%s", termo, cat_id)
            return _cache_categorias[termo]
    except Exception as exc:
        log.warning("  Erro ao buscar categoria PR | termo='%s' | %s", termo, exc)

    _cache_categorias[termo] = None
    return None


def buscar_precos_pr(
    session: requests.Session,
    produto: dict,
) -> list[dict]:
    """Busca preços na API do PR para um produto."""
    termo      = produto["busca"]
    cat_id     = buscar_categoria_pr(session, termo)

    params = {
        "termo": termo,
        "local": LOCAL,
        "raio":  RAIO,
        "data":  DATA_DIAS,
        "ordem": ORDEM,
    }
    if cat_id:
        params["categoria"] = cat_id

    url = f"{BASE_URL}/produtos"
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

    detectar_envenenamento(itens, LOCAL)

    resultados = []
    for item in itens[:MAX_RESULTADOS]:
        estab = item.get("estabelecimento", {})
        preco = float(item.get("valor") or 0)
        if preco <= 0:
            continue

        desc  = item.get("desc") or termo
        qtd, unidade_qtd = extrair_quantidade(desc)

        # Unidade: prioriza a do regex de qtd (ex: "500GR" → "GR"),
        # senão tenta detectar standalone na descrição, senão "UN".
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
    """
    Se MODO_CRON: ordena alfabético e fatia [GRUPO_INICIO:GRUPO_FIM]
    Senão: filtra por ativo=true
    """
    if MODO_CRON:
        resp = (sb.table("ADSef_produtos")
                  .select("id, nome, busca, categoria")
                  .order("nome", desc=False)
                  .execute())
        todos = resp.data or []
        produtos = todos[GRUPO_INICIO:GRUPO_FIM]
        log.info("Modo CRON: %d produtos no grupo [%d:%d] de %d totais",
                 len(produtos), GRUPO_INICIO, GRUPO_FIM, len(todos))
    else:
        resp = (sb.table("ADSef_produtos")
                  .select("id, nome, busca, categoria")
                  .eq("ativo", True)
                  .order("nome", desc=False)
                  .execute())
        produtos = resp.data or []
        # RETOMAR: pula os N primeiros ativos. Serve para continuar um run longo que
        # foi interrompido — o log de abort informa o indice exato para retomar.
        pular = int(os.environ.get("RETOMAR_DE", "0"))
        if pular > 0:
            produtos = produtos[pular:]
            log.info("Modo MANUAL: retomando do produto %d — %d restantes", pular, len(produtos))
        else:
            log.info("Modo MANUAL: %d produtos ativos carregados", len(produtos))

    return produtos


def carregar_estabelecimentos_existentes(sb: Client) -> set[str]:
    """Todos os codigos ja cadastrados — PAGINADO.

    O PostgREST devolve no maximo 1000 linhas por chamada. Com as lojas fabricadas
    de 23/07 a tabela passou de 76 mil linhas, entao sem paginar so as 1000 primeiras
    entravam no set: todas as outras pareciam novas, o scraper tentava cadastrar e
    levava 409 em cada uma. Ver docs/scraping-precos.md §10.1.

    Filtra envenenado=false porque as lojas fabricadas nao existem no mundo real: a
    API nunca as devolve numa resposta limpa, entao carrega-las so custaria requisicao
    (77 chamadas contra 1). A paginacao fica assim mesmo, para seguir correta se um dia
    o conjunto limpo passar de 1000.
    """
    codigos: set[str] = set()
    passo, inicio = 1000, 0
    while True:
        resp = (sb.table("ADSefPR_estabelecimentos")
                  .select("codigo")
                  .eq("envenenado", False)
                  .range(inicio, inicio + passo - 1)
                  .execute())
        lote = resp.data or []
        codigos.update(r["codigo"] for r in lote)
        if len(lote) < passo:
            break
        inicio += passo
    log.info("Estabelecimentos ja cadastrados: %d", len(codigos))
    return codigos


def abrir_coleta(sb: Client) -> int:
    fonte_label = "menorpreco_pr"
    if MODO_CRON:
        fonte_label += f"_grupo_{GRUPO_INICIO}-{GRUPO_FIM}"
    resp = (
        sb.table("ADSef_coletas")
        .insert({"status": "em_andamento", "fonte": fonte_label})
        .execute()
    )
    coleta_id = resp.data[0]["id"]
    log.info("Coleta iniciada | id=%d", coleta_id)
    return coleta_id


def fechar_coleta(sb: Client, coleta_id: int, total: int, erros: list) -> None:
    status = "sucesso" if not erros else ("erro_parcial" if total > 0 else "falha")
    sb.table("ADSef_coletas").update({
        "finalizado_em":   datetime.now(TZ).isoformat(),
        "status":          status,
        "total_registros": total,
        "erros":           json.dumps(erros, ensure_ascii=False) if erros else None,
    }).eq("id", coleta_id).execute()
    log.info("Coleta finalizada | id=%d status=%s total=%d erros=%d",
             coleta_id, status, total, len(erros))


def atualizar_cron_config(sb: Client, status: str, total: int) -> None:
    """Atualiza o registro em cron_config com o resultado da execução"""
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
            sb.table("ADSefPR_estabelecimentos").insert({
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
        sb.table("ADSefPR_precos")
        .upsert(rows, on_conflict="produto_id,regiao,data_coleta,codigo_estab,preco,nrdoc", ignore_duplicates=True)
        .execute()
    )
    return len(resp.data) if resp.data else 0


# ── Progresso e resumo (so no modo MANUAL / execucao local) ───────────────
#
# A fonte tolera ~25 produtos por sessao e depois passa a envenenar, INDEPENDENTE do
# ritmo: com 30s entre produtos deu 27, com 180s deu 23 (medido em 04 e 05/08). Logo o
# limite e de VOLUME acumulado, nao de velocidade — espacar mais nao compra cobertura.
# Mas o contador DECAI: apos ~40 min de silencio a fonte voltou a responder de verdade.
#
# Dai o desenho local: trabalha em BLOCOS, descansa entre eles, e ao ser envenenado
# recua e tenta de novo depois em vez de desistir. Isso e recuo diante de um sinal de
# "ja bastou", nao disfarce: mesmo IP, mesmo User-Agent, nada escondido.
#
# O progresso guarda a ULTIMA TENTATIVA de cada produto e a ordem de coleta e do mais
# desatualizado para o mais recente. Assim cada sessao continua de onde a anterior
# parou, o catalogo inteiro e coberto ao longo de varias sessoes e depois recomeca
# sozinho — sem reset por data, que faria repetir eternamente os primeiros produtos.
# Nada disso vale no Actions: la o loteamento (GRUPO_INICIO/GRUPO_FIM) faz esse papel.

FONTE_LABEL = "PE"
DIR_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)

# Quantos produtos por bloco antes de descansar, e quanto dura o descanso.
BLOCO        = int(os.environ.get("BLOCO", "20"))
DESCANSO_MIN = int(os.environ.get("DESCANSO_MIN", "60"))
# Quantos envenenamentos seguidos antes de desistir da sessao.
MAX_ENVENENAMENTOS = int(os.environ.get("MAX_ENVENENAMENTOS", "3"))


def _hoje() -> str:
    return datetime.now(TZ).date().isoformat()


def _arquivo_progresso() -> str:
    return os.path.join(DIR_LOGS, f"progresso-{FONTE_LABEL}.json")


def carregar_progresso() -> dict[str, str]:
    """{produto_id: timestamp da ultima tentativa}. Sem reset por data."""
    if MODO_CRON:
        return {}
    try:
        with open(_arquivo_progresso(), encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return {}
    return dados.get("tentativas") or {}


def salvar_progresso(tentativas: dict[str, str]) -> None:
    if MODO_CRON:
        return
    try:
        os.makedirs(DIR_LOGS, exist_ok=True)
        alvo = _arquivo_progresso()
        # grava em temporario e troca: se faltar energia no meio, o arquivo bom sobrevive
        temp = alvo + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump({"fonte": FONTE_LABEL, "atualizado_em": datetime.now(TZ).isoformat(),
                       "tentativas": tentativas}, f)
        os.replace(temp, alvo)
    except OSError as exc:
        log.warning("Nao consegui gravar o progresso: %s", exc)


def ordenar_por_desatualizacao(produtos: list[dict], tentativas: dict[str, str]) -> list[dict]:
    """Mais desatualizado primeiro; nunca tentado vem antes de tudo."""
    return sorted(produtos, key=lambda p: tentativas.get(str(p["id"]), ""))


def descansar(motivo: str) -> None:
    log.info("=== Descanso de %d min — %s ===", DESCANSO_MIN, motivo)
    log.info("    Retomando por volta de %s",
             (datetime.now(TZ) + timedelta(minutes=DESCANSO_MIN)).strftime("%H:%M"))
    time.sleep(DESCANSO_MIN * 60)


def escrever_resumo(linhas: list[str]) -> None:
    """Resumo curto do run, para leitura posterior sem varrer o log inteiro."""
    texto = "\n".join(linhas)
    print("\n" + texto)
    if MODO_CRON:
        return
    try:
        os.makedirs(DIR_LOGS, exist_ok=True)
        alvo = os.path.join(DIR_LOGS, f"resumo-{FONTE_LABEL}-{_hoje()}.txt")
        with open(alvo, "a", encoding="utf-8") as f:
            f.write(texto + "\n\n")
        log.info("Resumo salvo em %s", alvo)
    except OSError as exc:
        log.warning("Nao consegui gravar o resumo: %s", exc)


# ── Main ──────────────────────────────────────────────────────

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
    log.info("=== Monitor Menor Preço PR — iniciando ===")
    log.info("    Local: %s | Raio: %dkm | Período: %dd", LOCAL, RAIO, DATA_DIAS)

    sb           = conectar_supabase()
    produtos     = _com_retry(lambda: carregar_produtos(sb), descricao="carregar_produtos")
    existentes   = _com_retry(lambda: carregar_estabelecimentos_existentes(sb), descricao="carregar_estabelecimentos")

    if not produtos:
        log.warning("Nenhum produto para coletar nesse grupo/filtro.")
        atualizar_cron_config(sb, "sucesso", 0)
        sys.exit(0)

    # Coleta do mais desatualizado para o mais recente, para o catalogo rodar inteiro
    # ao longo das sessoes em vez de repetir sempre os primeiros.
    tentativas = carregar_progresso()
    total_catalogo = len(produtos)
    if not MODO_CRON:
        produtos = ordenar_por_desatualizacao(produtos, tentativas)
        nunca = sum(1 for p in produtos if str(p["id"]) not in tentativas)
        log.info("Ordem por desatualizacao | %d produtos, %d nunca coletados", total_catalogo, nunca)
        log.info("Blocos de %d produtos, descanso de %d min entre eles", BLOCO, DESCANSO_MIN)

    session     = requests.Session()
    coleta_id   = abrir_coleta(sb)
    total_geral = 0
    erros       = []

    envenenado  = None
    inicio_run  = datetime.now(TZ)
    sem_result  = []          # produtos que voltaram vazios
    com_result  = 0
    processados = 0
    lojas_novas = 0
    blocos      = 0
    n_envenen   = 0           # quantas vezes a fonte envenenou nesta sessao

    i = 0
    no_bloco = 0
    seguidas = 0              # envenenamentos consecutivos sem nenhum produto no meio

    while i < len(produtos):
        produto    = produtos[i]
        produto_id = produto["id"]
        nome       = produto["nome"]

        log.info("Buscando [%d/%d] | id=%d nome='%s' termo='%s'",
                 i + 1, len(produtos), produto_id, nome, produto["busca"])

        try:
            registros = buscar_precos_pr(session, produto)

            if registros:
                antes_lojas = len(existentes)
                existentes = registrar_estabelecimentos_novos(sb, registros, existentes)
                lojas_novas += len(existentes) - antes_lojas

            inseridos = inserir_precos(sb, coleta_id, produto_id, registros)
            total_geral += inseridos
            log.info("  → %d encontrados | %d inseridos", len(registros), inseridos)

            if registros:
                com_result += 1
            else:
                sem_result.append(nome)

            processados += 1
            tentativas[str(produto_id)] = datetime.now(TZ).isoformat()
            salvar_progresso(tentativas)
            i += 1
            no_bloco += 1
            seguidas = 0

        except RespostaEnvenenada as exc:
            n_envenen += 1
            seguidas  += 1
            log.warning("Fonte envenenou (%s) — %d vez(es) nesta sessao", exc, n_envenen)

            # No Actions e bloqueio de faixa, permanente: nao adianta esperar.
            # Local e quota temporaria, entao recua e tenta o mesmo produto depois.
            if MODO_CRON or seguidas >= MAX_ENVENENAMENTOS:
                envenenado = str(exc)
                log.error("=== ENCERRANDO — %s ===",
                          "modo CRON" if MODO_CRON
                          else f"{seguidas} envenenamentos seguidos, a quota nao se recuperou")
                log.error("    Os %d registros ja gravados sao reais e foram mantidos.", total_geral)
                erros.append(f"resposta envenenada: {exc}")
                break

            descansar(f"quota atingida no produto '{nome}'")
            no_bloco = 0
            continue          # nao avanca: refaz este produto apos o descanso

        except Exception as exc:
            msg = f"produto_id={produto_id} nome={nome}: {exc}"
            log.error("ERRO | %s", msg)
            erros.append(msg)
            i += 1
            no_bloco += 1

        # Fim de bloco: descansa antes de seguir, para nao esbarrar na quota.
        if not MODO_CRON and no_bloco >= BLOCO and i < len(produtos):
            blocos += 1
            descansar(f"bloco {blocos} concluido ({no_bloco} produtos)")
            no_bloco = 0

        time.sleep(SLEEP_REQUESTS)

    fechar_coleta(sb, coleta_id, total_geral, erros)

    status_final = ("falha" if envenenado
                    else "sucesso" if not erros
                    else "erro_parcial" if total_geral > 0
                    else "falha")

    # ── Resumo ───────────────────────────────────────────────────────────
    fim = datetime.now(TZ)
    dur = int((fim - inicio_run).total_seconds() // 60)
    resumo = [
        "=" * 62,
        "RESUMO — Insumos PE (Menor Preco PR)",
        f"Inicio {inicio_run:%d/%m %H:%M}   Fim {fim:%d/%m %H:%M}   ({dur} min)",
        f"Modo {'CRON' if MODO_CRON else 'MANUAL'} | pausa {SLEEP_REQUESTS}s | "
        f"bloco {BLOCO} | descanso {DESCANSO_MIN}min | coleta id={coleta_id}",
        "-" * 62,
        f"Status          : {status_final}",
        f"Linhas gravadas : {total_geral}",
        f"Produtos nesta sessao : {processados}  (com preco: {com_result} | vazios: {len(sem_result)})",
        f"Blocos concluidos     : {blocos}",
        f"Envenenamentos        : {n_envenen}"
        + ("  <- a quota se recuperou apos o descanso" if n_envenen and not envenenado else ""),
        f"Lojas novas     : {lojas_novas}",
    ]
    if envenenado:
        resumo += [
            "-" * 62,
            f"ENCERRADO por envenenamento persistente ({envenenado})",
            "O que ja foi gravado e real. Rode de novo depois: continua de onde parou.",
        ]
    if erros:
        resumo += ["-" * 62, f"ERROS ({len(erros)}):"] + [f"  - {e[:110]}" for e in erros[:10]]
    if not MODO_CRON:
        nunca_ainda = sum(1 for p in produtos if str(p["id"]) not in tentativas)
        resumo += [
            "-" * 62,
            f"CATALOGO: {total_catalogo - nunca_ainda} de {total_catalogo} produtos ja coletados"
            + (f" — {nunca_ainda} nunca vistos" if nunca_ainda else " — COBERTURA COMPLETA"),
            "A proxima sessao continua pelos mais desatualizados.",
        ]
    if sem_result:
        resumo += ["-" * 62, f"SEM RESULTADO ({len(sem_result)}):"] + [f"  {n}" for n in sem_result]
    resumo.append("=" * 62)
    escrever_resumo(resumo)

    atualizar_cron_config(sb, status_final, total_geral)
    log.info("=== Concluído | %d registros novos ===", total_geral)

    if envenenado or (total_geral == 0 and erros):
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
