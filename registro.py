"""Espelha a saida de um teste em logs/, alem da tela.

Os testes desta pasta respondem perguntas que custam requisicoes a uma fonte com
orcamento escasso — e ate 06/09/2026 eles so imprimiam na tela. Fechada a janela, o
resultado se perdia e a unica saida era gastar as requisicoes de novo. Agora fica
gravado: o custo ja foi pago uma vez.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Recife")


class _Espelho:
    def __init__(self, *destinos):
        self._destinos = destinos

    def write(self, texto):
        for d in self._destinos:
            d.write(texto)
            d.flush()

    def flush(self):
        for d in self._destinos:
            d.flush()


def tee(nome: str) -> str:
    """Passa a gravar tudo que for impresso em logs/<nome>-<data>.txt. Devolve o caminho."""
    raiz = os.path.dirname(os.path.abspath(__file__))
    logs = os.path.join(raiz, "logs")
    os.makedirs(logs, exist_ok=True)
    agora = datetime.now(TZ)
    caminho = os.path.join(logs, f"{nome}-{agora:%Y-%m-%d}.txt")

    f = open(caminho, "a", encoding="utf-8")
    f.write(f"\n{'='*62}\n{nome} — {agora:%d/%m/%Y %H:%M}\n{'='*62}\n")
    sys.stdout = _Espelho(sys.__stdout__, f)
    return caminho
