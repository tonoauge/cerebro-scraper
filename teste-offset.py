"""Teste do `offset` — a API pagina, ou estamos levando so a primeira pagina?

O `scraperFrutasPE.py` afirma num comentario que "a API nao tem paginacao" e por isso
usa MAX_RESULTADOS=200 sem nunca pedir a pagina seguinte. Mas duas medidas de 04/09
sugerem que a afirmacao esta errada:

  - a consulta `Uva` responde `total=1122` e devolve 50 itens. Estamos levando 4%;
  - o bundle da SPA do menorpreco monta a URL com `offset`:
        void 0 != n.searchParams.offset && (l += "&offset=" + n.searchParams.offset)

Se `offset` funcionar, cada requisicao continua trazendo ~50 itens, mas os termos
produtivos deixam de ficar limitados a primeira pagina. Para uma coleta cujo objetivo e
amostragem preco/dia/loja com orcamento escasso de requisicoes, isso muda o desenho: em
vez de rodizio entre 16 termos, profundidade nos que rendem.

    python teste-offset.py [TERMO]        # padrao: Uva

Custo: 1 requisicao para a sonda + 3 do teste. Bloqueado, ele sai na sonda.
"""

import argparse
import sys
import time

import requests

sys.path.insert(0, "scraper")
import registro
import scraperFrutasPE as sp   # LOCAIS, RAIO, DATA_DIAS, HEADERS, detector

ap = argparse.ArgumentParser()
ap.add_argument("termo", nargs="?", default="Uva")
ap.add_argument("--ponto", default=sp.LOCAIS[0], help="geohash do centro de captacao")
ap.add_argument("--dias", type=int, default=sp.DATA_DIAS, help="janela retroativa")
args = ap.parse_args()
TERMO = args.termo

_arquivo_log = registro.tee("teste-offset")   # a resposta custou requisicao: fica gravada
PONTO = args.ponto
DIAS = args.dias
_sessao = requests.Session()


def consultar(offset: int | None) -> dict:
    params = {"termo": TERMO, "local": PONTO, "raio": sp.RAIO,
              "data": DIAS, "ordem": 1}
    if offset is not None:
        params["offset"] = offset
    r = _sessao.get(f"{sp.BASE_URL}/produtos", headers=sp.HEADERS,
                    params=params, timeout=30)
    r.raise_for_status()
    return r.json() or {}


def identidade(item: dict) -> tuple:
    est = item.get("estabelecimento") or {}
    return (est.get("codigo"), item.get("nrdoc"), item.get("desc"), item.get("valor"))


print(f"termo={TERMO!r}  ponto={PONTO}  raio={sp.RAIO}km  janela={DIAS}d\n")

# ── Sonda: bloqueado, o teste nao distingue nada e nao vale gastar cota ──────
base = consultar(None)
itens0 = base.get("produtos") or []
if not itens0:
    print("SONDA: a fonte nao devolveu itens para este termo. Tente outro.")
    raise SystemExit(0)
try:
    sp.detectar_envenenamento(itens0, PONTO)
except sp.RespostaEnvenenada as exc:
    print(f"SONDA: BLOQUEADO ({exc})")
    print("Envenenada, a resposta e fabricada e a comparacao nao significa nada.")
    print("O bloqueio cai na virada do dia UTC — 21:00 em Recife. Rode depois disso.")
    raise SystemExit(0)

total = base.get("total")
print(f"SONDA: limpa | total={total} itens_devolvidos={len(itens0)}")

# `total` NAO e o numero de itens que daria para buscar: numa medida de 06/09 a mesma
# consulta trouxe 9 itens com total=74, e 9 nao e teto de nada. O mais provavel e que
# `total` conte registros de preco e a lista devolva produtos distintos. Comparar os dois
# como se fossem a mesma unidade so gera falso "estamos levando 12%".

PAGINA_APARENTE = 50   # maior lista ja observada; varias consultas param exatamente aqui

if len(itens0) < PAGINA_APARENTE:
    print(f"""
NAO DA PARA CONCLUIR com esta consulta.

  Ela devolveu {len(itens0)} itens, abaixo do teto aparente de {PAGINA_APARENTE}. Num conjunto que
  cabe inteiro na primeira resposta, pedir a pagina seguinte devolve vazio por
  esgotamento — o que e indistinguivel de "a API nao pagina".

  O teste so decide numa consulta que ENCOSTE no teto. Tente um termo/ponto mais
  densos, por exemplo:

      python teste-offset.py Uva --ponto 7n74tuuve --dias 3

  (essa combinacao devolveu exatamente 50 itens em 04/09)""")
    raise SystemExit(0)

# ── O teste, agora com direito de concluir ──────────────────────────────────
resultados = {0: {identidade(i) for i in itens0}}
for off in (len(itens0), len(itens0) * 2):
    time.sleep(5)
    d = consultar(off)
    its = d.get("produtos") or []
    resultados[off] = {identidade(i) for i in its}
    novos = resultados[off] - resultados[0]
    print(f"  offset={off:<4} itens={len(its):3}  novos em relacao a pagina 1: {len(novos):3}")

pag2 = resultados.get(len(itens0), set())
print()
if pag2 and pag2 == resultados[0]:
    print("VEREDITO: offset e IGNORADO — devolveu a mesma pagina.")
    print("  Nao ha paginacao. Ir alem do teto so variando termo, ponto ou janela.")
elif not pag2:
    print("VEREDITO: offset HONRADO, porem sem itens alem do teto.")
    print("  Ignora-lo devolveria a mesma pagina, nao vazio — entao o parametro e lido.")
    print("  Mas nada vem depois da primeira pagina. Na pratica, sem paginacao util.")
else:
    print("VEREDITO: A API PAGINA. offset traz itens novos.")
    print(f"  Cada pagina custa 1 requisicao e rende ate {len(itens0)} itens.")
    print("  Rever o desenho: profundidade nos termos que rendem, em vez de rodizio raso,")
    print("  e rever MAX_RESULTADOS e o comentario 'API nao tem paginacao'.")
