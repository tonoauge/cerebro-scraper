"""Teste do geohash — roda SÓ quando a fonte está bloqueada.

Responde a pergunta que ficou aberta em 03/09/2026: o envenenamento distingue um
ponto QUALQUER de um ponto que a própria fonte emitiu?

A §10.3 do doc já mostrou que o formato não basta — `7n74tuuvek0`, canônico de 11
dígitos, foi envenenado 47/47 em 28/08. Mas ele é um ponto que nós escolhemos. Os
pontos abaixo são geohashes de ESTABELECIMENTO colhidos da resposta limpa da própria
API em 04/09 — mesmo formato, origem diferente. Se algum deles voltar limpo enquanto
os nossos voltam envenenados, o discriminante é a origem do ponto.

Só faz sentido rodar BLOQUEADO: com a fonte liberada, tudo volta limpo e o teste não
distingue nada — foi exatamente esse o erro de leitura de 03/09.

    python teste-geohash.py

Custo: 1 requisição para a sonda + 4 do A/B.
"""

import sys, time

import requests

sys.path.insert(0, "scraper")
import scraperPE as sp   # reaproveita HEADERS, RAIO, DATA_DIAS e o mesmo cliente HTTP

# requests, e nao urllib: os HEADERS do scraper pedem gzip e so o requests descomprime.
_sessao = requests.Session()

TERMO = sys.argv[1] if len(sys.argv) > 1 else "Uva"

# Colhidos em 04/09/2026 da resposta limpa: geohash do estabelecimento, 11 chars, final 0.
# Duas perguntas, uma por eixo. COMPRIMENTO: 9 e o que a SPA envia, 11 e o que a fonte
# devolve por loja. ORIGEM: ponto geografico nosso contra geohash de um estabelecimento.
# O centro de captacao legitimo e um ponto geografico — loja entra aqui so como controle.
PONTOS_DA_FONTE = [
    ("centro de sempre, 9 chars",     "7n74tuuve"),
    ("ponto de frutas, 9 chars",      "7n754bpeh"),
    ("loja ATACADAO, 11 (controle)",  "7n74tsjskd0"),
]
# Fixos de proposito: se seguissem sp.LOCAL, a comparacao se perderia quando ele mudasse.
PONTOS_NOSSOS = [
    ("nosso, truncado 8 digitos",      "7n74tuuv"),
    ("nosso, 11 digitos escolhido",    "7n74tuuvek0"),
]


def consultar(local: str) -> tuple[int, int]:
    params = {"termo": TERMO, "local": local, "raio": sp.RAIO,
              "data": sp.DATA_DIAS, "ordem": 2}
    r = _sessao.get(f"{sp.BASE_URL}/produtos", headers=sp.HEADERS, params=params, timeout=30)
    r.raise_for_status()
    dados = r.json()
    itens = dados.get("produtos") or []
    ecoados = sum(1 for i in itens if (i.get("local") or "") == local)
    return len(itens), ecoados


def veredito(n: int, eco: int) -> str:
    if n == 0:
        return "SEM ITENS"
    return "ENVENENADO" if eco / n >= 0.5 else "LIMPO"


print(f"termo={TERMO!r}  raio={sp.RAIO}km  janela={sp.DATA_DIAS}d\n")

n, eco = consultar(sp.LOCAL)
estado = veredito(n, eco)
print(f"SONDA  {sp.LOCAL}  itens={n} ecoados={eco}  ->  {estado}\n")

if estado != "ENVENENADO":
    print("A fonte esta LIBERADA agora. Este teste so distingue algo com ela bloqueada:")
    print("liberada, todo ponto volta limpo e o resultado nao significa nada.")
    print("Rode de novo quando a sonda acusar bloqueio.")
    raise SystemExit(0)

print("Bloqueada — e a janela em que o teste vale. Comparando origens:\n")
resultados = []
for rotulo, ponto in PONTOS_DA_FONTE + PONTOS_NOSSOS:
    time.sleep(5)
    try:
        n, eco = consultar(ponto)
        v = veredito(n, eco)
    except Exception as exc:
        n, eco, v = 0, 0, f"ERRO {exc}"
    resultados.append((rotulo, ponto, v))
    print(f"  {rotulo:32} {ponto:12} itens={n:3} ecoados={eco:3}  ->  {v}")

limpos = [r for r in resultados if r[2] == "LIMPO"]
print()
if not limpos:
    print("VEREDITO: nenhum ponto escapou. A origem do geohash NAO e o discriminante —")
    print("  confirma a leitura da secao 10.3. Parar de investigar o geohash.")
else:
    print("VEREDITO: escaparam ->", ", ".join(f"{r[0]} ({r[1]})" for r in limpos))
    print("  A ORIGEM DO PONTO IMPORTA. Trocar LOCAL por um ponto emitido pela fonte")
    print("  e colher pontos novos periodicamente da propria resposta limpa.")
