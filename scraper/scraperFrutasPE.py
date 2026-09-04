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

# Falhas de requisicao acumuladas na sessao. Sem isso, 429 e erro de rede viram
# lista vazia e a coleta termina verde, indistinguivel de "a fonte nao tem nada".
FALHAS_REQUISICAO: list[str] = []


def registrar_falha(motivo: str) -> None:
    FALHAS_REQUISICAO.append(motivo)
    log.error("Falha de requisicao | %s", motivo)


# O cliente do Supabase loga uma linha por chamada HTTP, o que domina o log e o torna
# ilegivel num run longo. Erros de rede continuam aparecendo (nivel WARNING).
for _ruido in ("httpx", "httpcore", "hpack", "urllib3"):
    logging.getLogger(_ruido).setLevel(logging.WARNING)

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
# Pausa entre consultas, ajustavel por env para o run local poder ir mais devagar.
#
# CUIDADO com o padrao: aqui a pausa ocorre 4x por produto (uma por ponto em LOCAIS,
# mais uma no fim do produto). Com 16 produtos e o timeout de 30 min do job:
#     15s -> 16 x 4 x 15s = 16 min  (folga boa)
#     30s -> 16 x 4 x 30s = 32 min  (ESTOURA o timeout e deixa o lote em 'despachado')
# Por isso o padrao segue 15s, ao contrario do scraperPE.py que usa 30s. Local (modo
# MANUAL, sem timeout) usa SLEEP_REQUESTS=180 pelo wrapper.
SLEEP_REQUESTS = int(os.environ.get("SLEEP_REQUESTS", "15"))
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

# ── Cache de categorias PR ────────────────────────────────────────────────
# Ate 02/09/2026 este cache vivia so em memoria. Aqui ele acerta pouco: a chave inclui o
# ponto, e cada produto e consultado nos 3 de LOCAIS, entao sao 16 x 3 = 48 descobertas
# por sessao — metade das 96 requisicoes do run.
#
# Persistindo em disco, da segunda sessao em diante frutas cai de 6 para 3 requisicoes por
# produto. Mesmo raciocinio do scraperPE.py: a fonte corta por volume de requisicoes, entao
# cada chamada economizada e uma chance a menos de tropecar.
#
# logs/ esta no .gitignore — nada vai para o repo publico. Apagar o arquivo volta ao
# comportamento antigo.
# Medido em 03/09/2026: o ID de categoria NAO depende do ponto — 'Uva' devolve 55 e
# 'Amistar Top' devolve 18 nos tres LOCAIS e ate em Curitiba. Por isso a chave e so o
# termo, e o cache passa a acertar entre pontos (e entre as duas fontes PE, que
# compartilham cfru_produtos). Chaves antigas no formato '<local>|<termo>' continuam
# sendo lidas — carregar_categorias() as normaliza.
_cache_categorias: dict[str, int | None] = {}     # da sessao (inclui negativos)
_categorias_disco: dict[str, dict] = {}           # o que veio e o que vai para o arquivo
_cat_do_cache = 0
_cat_consultadas = 0

CATEGORIAS_TTL_DIAS = 90

# A API ordena as categorias por quantidade, e `categorias[0]` erra justamente no termo
# mais amplo: 'Uva' devolve Bebidas(215) antes de Hortifruti(123). Medido em 03/09 com
# categoria=55 vieram 50 itens e ZERO com NCM 0806 (so Fanta e suco, tudo descartado
# depois pela allowlist de NCM); com categoria=4 vieram 47 itens, 46 deles NCM 08061000.
# Os outros 15 termos do catalogo ja caem em Hortifruti ou nao tem categoria.
CATEGORIAS_PREFERIDAS = ("hortifruti",)


# ── Detecção de resposta envenenada ───────────────────────────────────────
# Desde 2026-07-23 a API devolve dados FABRICADOS (municípios e lojas com letras
# trocadas, UFs sorteadas em terços, preços aleatórios) para as chamadas vindas
# dos IPs do GitHub Actions. Do mesmo endpoint, com os mesmos parâmetros, um IP
# residencial recebe dado real — o corte é por IP.
#
# Assinatura infalível: no dado real, `local` é o geohash do ESTABELECIMENTO, com
# 11 caracteres e distinto a cada loja. No dado fabricado, a API ecoa de volta o
# geohash que ENVIAMOS na consulta. Igualdade entre os dois não acontece por acaso.

class RespostaEnvenenada(Exception):
    """A API devolveu dados sintéticos — abortar sem gravar nada."""


def detectar_envenenamento(itens: list[dict], local: str) -> None:
    """Levanta RespostaEnvenenada quando a resposta e fabricada.

    Dois sinais independentes, e o NCM decide:

    - **NCM sintetico** — no dado fabricado ele tem 9 digitos e comeca em `320`; no
      real tem 8. Medido em 04/09/2026 sobre 1.000 linhas de cada lado em
      `cfru_precos_pe`: 100% de um jeito e 100% do outro. Nao depende do geohash.
    - **Geohash ecoado** — a API devolve o ponto que ENVIAMOS em vez do ponto da loja.

    O eco sozinho deixou de bastar: desde que `LOCAL` passou a ser um ponto emitido
    pela fonte (o geohash de uma loja real), os itens daquela loja ecoam o valor da
    consulta por direito. Num produto vendido so ali, o eco passaria de 50% e abortaria
    uma sessao limpa. Por isso o eco so condena acompanhado do NCM sintetico.
    """
    if not itens:
        return

    total     = len(itens)
    ecoados   = sum(1 for it in itens if (it.get("local") or "") == local)
    sinteticos = sum(1 for it in itens if len(str(it.get("ncm") or "")) == 9)

    if sinteticos / total >= 0.5:
        raise RespostaEnvenenada(
            f"NCM sintetico em {sinteticos}/{total} itens"
            f" (local='{local}' ecoado em {ecoados}/{total})"
        )

    if ecoados / total >= 0.5:
        # Eco alto sem NCM sintetico: e a nossa propria loja de referencia aparecendo.
        log.info("  Eco alto sem NCM sintetico (%d/%d) — consulta centrada em loja real,"
                 " dado tratado como limpo", ecoados, total)

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

def escolher_categoria(categorias: list[dict]) -> dict | None:
    """A preferida quando estiver na lista; senao a primeira, como antes."""
    validas = [c for c in categorias if isinstance(c, dict)]
    if not validas:
        return None
    for pref in CATEGORIAS_PREFERIDAS:
        for c in validas:
            if pref in str(c.get("desc") or "").strip().lower():
                return c
    return validas[0]


def buscar_categoria_pr(session: requests.Session, termo: str, local: str) -> int | None:
    """ID de categoria da API do PR para o termo, do disco quando já conhecido.

    O `local` ainda vai na requisição (a API o exige), mas não entra na chave do
    cache — ver o comentário em CATEGORIAS_PREFERIDAS.
    """
    global _cat_do_cache, _cat_consultadas

    if termo in _cache_categorias:
        return _cache_categorias[termo]

    reg = _categorias_disco.get(termo)
    if reg:
        _cache_categorias[termo] = reg["id"]
        _cat_do_cache += 1
        return reg["id"]

    _cat_consultadas += 1
    url = f"{BASE_URL}/categorias"
    params = {"termo": termo, "local": local, "raio": RAIO, "data": DATA_DIAS}
    try:
        r = session.get(url, headers=HEADERS, params=params, timeout=20)
        r.raise_for_status()
        dados = r.json()
        categorias = dados.get("categorias") or dados.get("resultado") or []
        escolhida = escolher_categoria(categorias)
        if escolhida:
            cat_id = escolhida.get("id") or escolhida.get("codigo")
            valor = int(cat_id) if cat_id else None
            _cache_categorias[termo] = valor
            # Só o positivo vai para o disco: um "não achei" pode ter vindo de resposta
            # envenenada ou erro de rede, e gravado envenenaria o cache para sempre.
            if valor is not None:
                _categorias_disco[termo] = {"id": valor,
                                            "visto_em": datetime.now(TZ).isoformat()}
            log.info("  Categoria PR escolhida | termo='%s' categoria_id=%s desc='%s' (de %d opcao(oes))",
                     termo, cat_id, escolhida.get("desc"), len(categorias))
            return valor
    except Exception as exc:
        log.warning("  Erro ao buscar categoria PR | termo='%s' | %s", termo, exc)

    _cache_categorias[termo] = None
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
                    registrar_falha(f"429 persistente | termo={termo}")
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
                registrar_falha(f"HTTP error | termo={termo} | {exc}")
                return []
        except ValueError:
            registrar_falha(f"JSON invalido | termo={termo}")
            return []

    if dados is None:
        registrar_falha(f"sem payload apos {MAX_RETRIES} tentativas | termo={termo}")
        return []

    itens = dados.get("produtos") or []
    total = dados.get("total", 0)
    pmin  = dados.get("precos", {}).get("min")
    pmax  = dados.get("precos", {}).get("max")
    log.info("  → %d resultados | min R$%s | max R$%s", total, pmin, pmax)

    detectar_envenenamento(itens, local)

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
    """Todos os codigos ja cadastrados — PAGINADO.

    O PostgREST devolve no maximo 1000 linhas por chamada. Com as lojas fabricadas
    de 23/07 a tabela passou de 28 mil linhas, entao sem paginar so as 1000 primeiras
    entravam no set: todas as outras pareciam novas, o scraper tentava cadastrar e
    levava 409 em cada uma. Ver docs/scraping-precos.md §10.1.

    Filtra envenenado=false porque as lojas fabricadas nao existem no mundo real: a
    API nunca as devolve numa resposta limpa, entao carrega-las so custaria requisicao.
    A paginacao fica assim mesmo, para seguir correta se o conjunto limpo passar de 1000.
    """
    codigos: set[str] = set()
    passo, inicio = 1000, 0
    while True:
        resp = (sb.table("cfru_estabelecimentos")
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


def fechar_coleta(sb: Client, coleta_id: int, total: int, erros: list,
                  produtos_tentados: int = 0, total_encontrados: int | None = None) -> str:
    """Fecha a coleta e devolve o status gravado.

    Consultar produtos e nao achar nada em nenhum deles nao e sucesso — e assim
    que uma fonte morre em silencio (frutas BA passou 5 meses em zero terminando
    verde). O gatilho e `encontrados`, nao `inseridos`: um re-run no mesmo dia
    insere 0 por deduplicacao e continua sendo uma coleta sadia.
    """
    erros = list(erros)
    vazia = (
        not erros
        and produtos_tentados > 0
        and total_encontrados is not None
        and total_encontrados == 0
    )
    if vazia:
        erros.append(
            f"coleta vazia: {produtos_tentados} produto(s) consultado(s), "
            f"nenhum resultado da fonte"
        )
        status = "erro_parcial"
    else:
        status = "sucesso" if not erros else ("erro_parcial" if total > 0 else "falha")
    sb.table("cfru_coletas").update({
        "finalizado_em":   datetime.now(TZ).isoformat(),
        "status":          status,
        "total_registros": total,
        "erros":           json.dumps(erros, ensure_ascii=False) if erros else None,
    }).eq("id", coleta_id).execute()
    log.info("Coleta finalizada | id=%d status=%s total=%d erros=%d",
             coleta_id, status, total, len(erros))
    return status


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


# ── Progresso e resumo (so no modo MANUAL / execucao local) ───────────────
#
# Um run local pode ser interrompido por abort de envenenamento, pela janela fechada
# ou pelo PC desligar. O arquivo de progresso guarda os ids ja tentados no dia, entao
# basta rodar de novo que ele continua de onde parou. Vira o dia, perde a validade
# sozinho. No Actions nao roda: la o loteamento ja faz esse papel.

FONTE_LABEL = "FRUTAS-PE"
DIR_LOGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)

# Quantos produtos por bloco antes de descansar, e quanto dura o descanso.
BLOCO        = int(os.environ.get("BLOCO", "20"))
DESCANSO_MIN = int(os.environ.get("DESCANSO_MIN", "60"))
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


def _arquivo_categorias() -> str:
    return os.path.join(DIR_LOGS, f"categorias-{FONTE_LABEL}.json")


def carregar_categorias() -> dict[str, dict]:
    """{'<termo>': {'id': int, 'visto_em': iso}}, sem as entradas vencidas.

    Aceita o formato antigo '<local>|<termo>' e o converte, para nao descartar um
    cache ja aquecido quando a chave mudou.
    """
    if MODO_CRON:
        return {}
    try:
        with open(_arquivo_categorias(), encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, ValueError):
        return {}
    limite = datetime.now(TZ) - timedelta(days=CATEGORIAS_TTL_DIAS)
    vivas: dict[str, dict] = {}
    for chave, reg in (dados.get("categorias") or {}).items():
        try:
            if datetime.fromisoformat(reg["visto_em"]) >= limite:
                vivas[chave.split("|")[-1]] = reg
        except (KeyError, TypeError, ValueError):
            continue
    return vivas


def salvar_categorias() -> None:
    if MODO_CRON or not _categorias_disco:
        return
    try:
        os.makedirs(DIR_LOGS, exist_ok=True)
        alvo = _arquivo_categorias()
        temp = alvo + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump({"fonte": FONTE_LABEL,
                       "atualizado_em": datetime.now(TZ).isoformat(),
                       "categorias": _categorias_disco}, f)
        os.replace(temp, alvo)
    except OSError as exc:
        log.warning("Nao consegui gravar o cache de categorias: %s", exc)


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
    log.info("=== Monitor de Frutas PE — iniciando ===")
    log.info("    Locais: %s | Raio: %dkm | Período: %dd | Ordens: %s", LOCAIS, RAIO, DATA_DIAS, ORDENS)

    sb         = conectar_supabase()
    produtos   = _com_retry(lambda: carregar_produtos(sb), descricao="carregar_produtos")
    existentes = _com_retry(lambda: carregar_estabelecimentos_existentes(sb), descricao="carregar_estabelecimentos")

    if not produtos:
        log.warning("Nenhum produto para coletar nesse grupo/filtro.")
        atualizar_cron_config(sb, "sucesso", 0)
        sys.exit(0)

    # Coleta do mais desatualizado para o mais recente, para o catalogo rodar inteiro
    # ao longo das sessoes em vez de repetir sempre os primeiros.
    tentativas = carregar_progresso()
    _categorias_disco.update(carregar_categorias())
    if _categorias_disco:
        log.info("Cache de categorias: %d pares (ponto, termo) conhecidos", len(_categorias_disco))
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
    encontrados = 0   # linhas que a fonte devolveu — distingue "fonte muda" de "tudo duplicado"

    envenenado  = None
    inicio_run  = datetime.now(TZ)
    sem_result  = []
    com_result  = 0
    processados = 0
    lojas_novas = 0
    blocos      = 0
    n_envenen   = 0

    idx = 0
    no_bloco = 0
    seguidas = 0

    while idx < len(produtos):
        produto    = produtos[idx]
        produto_id = produto["id"]
        nome       = produto["nome"]

        log.info("Buscando [%d/%d] | id=%d nome='%s' termo='%s'",
                 idx + 1, len(produtos), produto_id, nome, produto["busca"])

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
                antes_lojas = len(existentes)
                existentes = registrar_estabelecimentos_novos(sb, todos_registros, existentes)
                lojas_novas += len(existentes) - antes_lojas

            inseridos = inserir_precos(sb, coleta_id, produto_id, todos_registros)
            total_geral += inseridos
            encontrados += len(todos_registros)
            log.info("  → %d coletados únicos | %d inseridos", len(todos_registros), inseridos)

            if todos_registros:
                com_result += 1
            else:
                sem_result.append(nome)

            processados += 1
            tentativas[str(produto_id)] = datetime.now(TZ).isoformat()
            salvar_progresso(tentativas)
            salvar_categorias()   # junto do progresso: um abort no meio nao perde o cache
            idx += 1
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
            idx += 1
            no_bloco += 1

        if not MODO_CRON and no_bloco >= BLOCO and idx < len(produtos):
            blocos += 1
            descansar(f"bloco {blocos} concluido ({no_bloco} produtos)")
            no_bloco = 0

        time.sleep(SLEEP_REQUESTS)

    # Gravação final: o último produto pode ter descoberto uma categoria e sido
    # envenenado logo em seguida, sem passar pelo salvamento de dentro do laço.
    salvar_categorias()

    # Falhas de requisicao entram no status: 429 e erro de rede nao podem sair
    # verdes so porque o produto voltou "vazio".
    if FALHAS_REQUISICAO:
        amostra = "; ".join(FALHAS_REQUISICAO[:5])
        extra = "" if len(FALHAS_REQUISICAO) <= 5 else f" (+{len(FALHAS_REQUISICAO) - 5})"
        erros.append(f"{len(FALHAS_REQUISICAO)} falha(s) de requisicao: {amostra}{extra}")

    status_coleta = fechar_coleta(sb, coleta_id, total_geral, erros,
                                  produtos_tentados=processados, total_encontrados=encontrados)

    # Envenenamento manda no status: a sessao pode ate ter gravado linhas reais
    # antes da parede, mas terminou por bloqueio da fonte.
    status_final = "falha" if envenenado else status_coleta

    # ── Resumo ───────────────────────────────────────────────────────────
    fim = datetime.now(TZ)
    dur = int((fim - inicio_run).total_seconds() // 60)
    resumo = [
        "=" * 62,
        "RESUMO — Frutas PE (Menor Preco PR)",
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
        f"Categorias      : {_cat_do_cache} do cache | {_cat_consultadas} consultadas"
        + ("  <- 3 requisicoes por produto" if _cat_consultadas == 0 and _cat_do_cache
           else "  <- cache enchendo" if _cat_do_cache else ""),
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
