"""Teste A/B do parametro `categoria` — 5 requisicoes, roda uma vez e responde duas coisas.

Contexto: cada produto custa hoje DUAS requisicoes, /categorias + /produtos. A fonte corta
por volume de requisicoes (nas 13 sessoes medidas a parede caiu entre 38 e 224), entao
economizar uma chamada por produto vale o dobro de produtos por sessao.

Este teste decide COMO economizar:

  1) O ID de categoria depende do ponto consultado?
     Se nao depender, o cache em disco pode ser chaveado so pelo termo e rende mais.

  2) O parametro `categoria` muda o resultado da busca?
     Se nao mudar, da para apagar a chamada de vez, em vez de cachear.

Rodar SOMENTE com a sonda de pre-voo limpa — o script confere isso sozinho e desiste se
o IP estiver bloqueado. Nao grava nada em lugar nenhum.

    python teste-categoria.py [TERMO]
"""
import os
import sys
import time
import importlib.util

import requests

import registro

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RAIZ = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(RAIZ, ".env.local")):
    for linha in open(os.path.join(RAIZ, ".env.local"), encoding="utf-8"):
        linha = linha.strip()
        if "=" in linha and not linha.startswith("#"):
            k, v = linha.split("=", 1)
            os.environ.setdefault(k, v)

# Reaproveita constantes e o detector do scraper em vez de duplicar as regras.
_spec = importlib.util.spec_from_file_location("sp", os.path.join(RAIZ, "scraper", "scraperPE.py"))
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)

TERMO = sys.argv[1] if len(sys.argv) > 1 else "Score"

_arquivo_log = registro.tee("teste-categoria")   # a resposta custou requisicao: fica gravada

PONTO_ATUAL = sp.LOCAL          # 8 digitos
PONTO_ANTIGO = "7n74tuuvek0"    # 11 digitos, o que usamos ate 28/08
PAUSA = 20


def get(caminho, params):
    r = requests.get(f"{sp.BASE_URL}/{caminho}", headers=sp.HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def categoria(local):
    dados = get("categorias", {"termo": TERMO, "local": local, "raio": sp.RAIO,
                               "data": sp.DATA_DIAS})
    cats = dados.get("categorias") or dados.get("resultado") or []
    if cats and isinstance(cats[0], dict):
        return cats[0].get("id") or cats[0].get("codigo")
    return None


def produtos(cat_id):
    params = {"termo": TERMO, "local": PONTO_ATUAL, "raio": sp.RAIO,
              "data": sp.DATA_DIAS, "ordem": sp.ORDEM}
    if cat_id:
        params["categoria"] = cat_id
    dados = get("produtos", params)
    itens = dados.get("produtos") or []
    sp.detectar_envenenamento(itens, PONTO_ATUAL)   # aborta se a fonte virar no meio
    chaves = {(i.get("codigo_estab") or (i.get("estabelecimento") or {}).get("codigo"),
               i.get("nrdoc"), i.get("valor")) for i in itens}
    return dados.get("total"), itens, chaves


print(f"termo={TERMO!r}  ponto atual={PONTO_ATUAL}  raio={sp.RAIO}  janela={sp.DATA_DIAS}d")
print("=" * 76)

print("\n[0/4] sonda de pre-voo")
liberado, detalhe = sp.sondar_bloqueio(requests.Session())
print(f"      {detalhe}")
if not liberado:
    print("\n  IP BLOQUEADO — teste cancelado, nenhuma requisicao gasta a toa.")
    print("  O bloqueio dura de 17 a 28 h. Tente mais tarde.")
    sys.exit(0)

try:
    time.sleep(PAUSA)
    print(f"\n[1/4] /categorias no ponto atual  ({PONTO_ATUAL}, {len(PONTO_ATUAL)} digitos)")
    id_atual = categoria(PONTO_ATUAL)
    print(f"      categoria_id = {id_atual}")

    time.sleep(PAUSA)
    print(f"\n[2/4] /categorias no ponto antigo ({PONTO_ANTIGO}, {len(PONTO_ANTIGO)} digitos)")
    id_antigo = categoria(PONTO_ANTIGO)
    print(f"      categoria_id = {id_antigo}")

    time.sleep(PAUSA)
    print(f"\n[3/4] /produtos COM categoria={id_atual}")
    tot_com, itens_com, chaves_com = produtos(id_atual)
    print(f"      total={tot_com}  itens={len(itens_com)}")

    time.sleep(PAUSA)
    print("\n[4/4] /produtos SEM categoria")
    tot_sem, itens_sem, chaves_sem = produtos(None)
    print(f"      total={tot_sem}  itens={len(itens_sem)}")
except sp.RespostaEnvenenada as exc:
    print(f"\n  A fonte envenenou no meio do teste ({exc}).")
    print("  Resultado inconclusivo — repetir noutra janela limpa.")
    sys.exit(0)

print("\n" + "=" * 76)
print("  CONCLUSOES")
print("=" * 76)

print("\n  1) O ID de categoria depende do ponto?")
if id_atual == id_antigo:
    print(f"     NAO — os dois pontos deram {id_atual}.")
    print("     -> o cache pode ser chaveado so pelo TERMO. Trocar a chave em")
    print("        buscar_categoria_pr(): f'{LOCAL}|{termo}'  ->  termo")
else:
    print(f"     SIM — {id_atual} no ponto atual, {id_antigo} no antigo.")
    print("     -> manter a chave como esta, com o ponto embutido.")

print("\n  2) O parametro `categoria` muda o resultado?")
if tot_com == tot_sem and chaves_com == chaves_sem:
    print(f"     NAO — mesmo total ({tot_com}) e exatamente os mesmos itens.")
    print("     -> a chamada /categorias e DISPENSAVEL. Mais simples que cachear:")
    print("        apagar buscar_categoria_pr() e parar de enviar `categoria`.")
elif tot_com == tot_sem:
    print(f"     Totais iguais ({tot_com}), mas os itens diferem em "
          f"{len(chaves_com ^ chaves_sem)} registros.")
    print("     -> nao apagar a chamada; ficar com o cache em disco.")
else:
    print(f"     SIM — com filtro {tot_com}, sem filtro {tot_sem} "
          f"(diferenca de {abs((tot_com or 0) - (tot_sem or 0))}).")
    print("     -> nao apagar a chamada; o cache em disco e o caminho certo.")

print("\n  O cache em disco ja esta no ar e funciona nos dois casos.")
print("  Estas conclusoes so dizem se da para simplificar ainda mais.")

print("\n" + "=" * 76)
print("  O IP ESTA LIMPO AGORA — aproveite a janela.")
print("  Rode em seguida:  rodar coleta de precos - PR.bat")
print("  A janela dura ate a proxima parede; depois dela sao 17 a 28 h de espera.")
print("=" * 76)
