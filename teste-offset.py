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

import sys
import time

import requests

sys.path.insert(0, "scraper")
import registro
import scraperFrutasPE as sp   # LOCAIS, RAIO, DATA_DIAS, HEADERS, detector

TERMO = sys.argv[1] if len(sys.argv) > 1 else "Uva"

_arquivo_log = registro.tee("teste-offset")   # a resposta custou requisicao: fica gravada
PONTO = sp.LOCAIS[0]
_sessao = requests.Session()


def consultar(offset: int | None) -> dict:
    params = {"termo": TERMO, "local": PONTO, "raio": sp.RAIO,
              "data": sp.DATA_DIAS, "ordem": 1}
    if offset is not None:
        params["offset"] = offset
    r = _sessao.get(f"{sp.BASE_URL}/produtos", headers=sp.HEADERS,
                    params=params, timeout=30)
    r.raise_for_status()
    return r.json() or {}


def identidade(item: dict) -> tuple:
    est = item.get("estabelecimento") or {}
    return (est.get("codigo"), item.get("nrdoc"), item.get("desc"), item.get("valor"))


print(f"termo={TERMO!r}  ponto={PONTO}  raio={sp.RAIO}km  janela={sp.DATA_DIAS}d\n")

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
if total and len(itens0) < total:
    print(f"        estamos levando {100*len(itens0)/total:.0f}% do que a fonte diz ter\n")

# ── O teste ─────────────────────────────────────────────────────────────────
resultados = {0: {identidade(i) for i in itens0}}
for off in (len(itens0), len(itens0) * 2):
    time.sleep(5)
    d = consultar(off)
    its = d.get("produtos") or []
    resultados[off] = {identidade(i) for i in its}
    novos = resultados[off] - resultados[0]
    print(f"  offset={off:<4} itens={len(its):3}  novos em relacao a pagina 1: {len(novos):3}")

pag2 = resultados.get(len(itens0), set())
if not pag2:
    print("\nVEREDITO: offset nao devolve nada — a paginacao nao existe mesmo,")
    print("  e o comentario do scraper esta certo. Manter o desenho atual.")
elif pag2 == resultados[0]:
    print("\nVEREDITO: offset e IGNORADO — devolve a mesma pagina.")
    print("  Sem paginacao real; a unica saida para ir alem dos 50 continua sendo")
    print("  variar termo, ponto ou janela.")
else:
    paginas = (total // len(itens0) + 1) if total and itens0 else "?"
    print(f"\nVEREDITO: A API PAGINA. offset traz itens novos.")
    print(f"  Para o termo {TERMO!r} isso significa ~{paginas} paginas de ~{len(itens0)} itens,")
    print(f"  ao mesmo custo de 1 requisicao por pagina.")
    print("  Rever o desenho: profundidade nos termos que rendem, em vez de rodizio raso.")
    print("  Rever tambem MAX_RESULTADOS e o comentario 'API nao tem paginacao'.")
