"""Espera uma coleta PE terminar e emenda a outra.

Os dois scrapers PE falam com a mesma fonte pelo mesmo IP. Rodar em paralelo dobra o
ritmo — cada processo pausa 60s por conta propria, entao juntos batem a fonte a cada
30s — e ainda embaralha a contagem de REQUISICOES, que e o instrumento com que medimos
onde a parede cai. Por isso: em fila, nunca junto.

    python encadear.py frutas     # espera insumos terminar, roda frutas
    python encadear.py insumos    # espera frutas terminar, roda insumos

O sinal de fim e o resumo do dia, que o scraper grava na ultima linha do main, e so
vale se for mais novo que o instante em que o encadeador subiu — um resumo de sessao
anterior do mesmo dia dispararia a segunda coleta na hora errada.

Deixe a janela aberta e va dormir. Se a primeira coleta morrer sem gravar resumo, este
script desiste depois de ESPERA_MAX_H em vez de disparar a segunda as cegas.
"""

import glob
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Recife")
RAIZ = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(RAIZ, "logs")
INTERVALO_S = 60

# Insumos leva ~5h40 quando a janela e generosa (343 min em 09/09). Frutas usa
# MAX_ENVENENAMENTOS=1 desde 04/09, entao encerra na primeira parede. A espera e
# generosa de proposito: esperar demais so adia, desistir cedo perde a janela.
ESPERA_MAX_H = 8

COLETAS = {
    # o que rodar : (prefixo do resumo que sinaliza o fim da OUTRA, runner)
    "insumos": ("resumo-FRUTAS-PE", "rodarPR.py"),
    "frutas":  ("resumo-PE",        "rodarFRUTASPE.py"),
}


def resumo_mais_novo(prefixo):
    """Mtime do resumo mais recente com esse prefixo, ou 0 se nao houver nenhum.

    Vigiar por prefixo, e nao pelo nome do dia: o resumo leva a data em que a sessao
    TERMINA. Uma coleta iniciada as 21:00 costuma virar a madrugada — a de 09/09 22:38
    gravou resumo-PE-2026-09-10.txt — e um vigia preso a data de hoje nunca veria o
    sinal, desistindo depois de horas sem disparar a segunda coleta.
    """
    arquivos = glob.glob(os.path.join(LOGS, prefixo + "-*.txt"))
    return max((os.path.getmtime(a) for a in arquivos), default=0)


def main():
    alvo_nome = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if alvo_nome not in COLETAS:
        print("uso: python encadear.py [" + " | ".join(COLETAS) + "]")
        sys.exit(2)

    prefixo, runner = COLETAS[alvo_nome]
    espera = "frutas" if alvo_nome == "insumos" else "insumos"
    inicio = datetime.now(TZ)
    limite = inicio + timedelta(hours=ESPERA_MAX_H)

    marca = resumo_mais_novo(prefixo)

    print("vou rodar " + alvo_nome + " quando " + espera + " terminar")
    print("sinal: um " + prefixo + "-*.txt mais novo que agora")
    print(f"inicio {inicio:%H:%M} | desiste as {limite:%H:%M} se nao houver sinal")
    print()

    while datetime.now(TZ) < limite:
        if resumo_mais_novo(prefixo) > marca:
            print()
            print(f"[{datetime.now(TZ):%H:%M}] {espera} concluiu. Emendando {alvo_nome}.")
            print()
            break
        print(f"[{datetime.now(TZ):%H:%M}] {espera} ainda rodando...")
        time.sleep(INTERVALO_S)
    else:
        print()
        print(f"[{datetime.now(TZ):%H:%M}] {ESPERA_MAX_H} h sem sinal de fim.")
        print(alvo_nome + " NAO foi disparado — melhor nada do que dois processos na fonte.")
        print("Veja a janela de " + espera + ": provavelmente morreu sem gravar resumo.")
        sys.exit(1)

    # o runner, nao o scraper direto: e ele que carrega o .env.local e fixa
    # SLEEP_REQUESTS / BLOCO / DESCANSO_MIN / MAX_ENVENENAMENTOS.
    os.chdir(RAIZ)
    os.execv(sys.executable, [sys.executable, os.path.join(RAIZ, runner)])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("interrompido — a segunda coleta nao foi disparada.")
