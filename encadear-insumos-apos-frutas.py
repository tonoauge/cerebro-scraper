"""Espera a sessao de frutas terminar e emenda a de insumos.

Os dois scrapers falam com a mesma fonte pelo mesmo IP. Rodar em paralelo dobra o
ritmo — cada processo pausa 60s por conta propria, entao juntos batem a fonte a cada
30s — e ainda embaralha a contagem de REQUISICOES, que e o instrumento com que
medimos onde a parede cai. Por isso: em fila, nunca junto.

O sinal de fim e o resumo do dia, que o scraper de frutas grava na ultima linha do
main. Enquanto ele nao aparecer, aqui so se espera.

    python encadear-insumos-apos-frutas.py

Deixe a janela aberta e va dormir. Se frutas morrer sem gravar resumo, este script
desiste depois de 3 h em vez de disparar insumos as cegas.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Recife")
RAIZ = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(RAIZ, "logs")
# Frutas usa MAX_ENVENENAMENTOS=3 (o padrao do scraper; rodarFRUTASPE nao fixa), e cada
# envenenamento custa um descanso de 60 min antes de refazer o produto. No pior caso a
# sessao passa de 3 h. A espera e generosa de proposito: esperar demais so adia, enquanto
# desistir cedo perde a janela.
ESPERA_MAX_H = 5
INTERVALO_S = 60


def resumo_de_hoje() -> str:
    hoje = datetime.now(TZ).date().isoformat()
    return os.path.join(LOGS, f"resumo-FRUTAS-PE-{hoje}.txt")


def main() -> None:
    alvo = resumo_de_hoje()
    inicio = datetime.now(TZ)
    limite = inicio + timedelta(hours=ESPERA_MAX_H)

    # So conta resumo gravado DEPOIS que este script subiu: um resumo de uma sessao
    # anterior do mesmo dia dispararia insumos na hora errada.
    ja_existia = os.path.exists(alvo)
    marca = os.path.getmtime(alvo) if ja_existia else 0

    print(f"aguardando frutas terminar — sinal: {os.path.basename(alvo)}")
    print(f"inicio {inicio:%H:%M} | desiste as {limite:%H:%M} se nao houver sinal\n")

    while datetime.now(TZ) < limite:
        if os.path.exists(alvo) and os.path.getmtime(alvo) > marca:
            print(f"\n[{datetime.now(TZ):%H:%M}] frutas concluiu. Emendando insumos.\n")
            break
        print(f"[{datetime.now(TZ):%H:%M}] frutas ainda rodando...")
        time.sleep(INTERVALO_S)
    else:
        print(f"\n[{datetime.now(TZ):%H:%M}] {ESPERA_MAX_H} h sem sinal de fim.")
        print("Insumos NAO foi disparado — melhor nada do que dois processos na fonte.")
        print("Veja a janela de frutas: ela provavelmente morreu sem gravar resumo.")
        sys.exit(1)

    # rodarPR.py, nao o scraper direto: e ele que carrega o .env.local e fixa
    # SLEEP_REQUESTS/BLOCO/DESCANSO_MIN/MAX_ENVENENAMENTOS. Chamar o scraper direto
    # subiria sem credencial do Supabase e com o ritmo errado.
    os.chdir(RAIZ)
    os.execv(sys.executable, [sys.executable, os.path.join(RAIZ, "rodarPR.py")])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrompido — insumos nao foi disparado.")
