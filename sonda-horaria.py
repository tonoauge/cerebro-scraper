"""Sonda horaria do Nota Parana — mede QUANDO o bloqueio cai.

Existe para resolver uma contradicao aberta (docs/scraping-precos.md §10.6):

  - a secao 10.6 mediu a transicao ENVENENADO -> LIMPO em 25 minutos;
  - a secao A6 do plano anterior mediu bloqueio a 10,8 h e 17,1 h, limpo a 28,4 h.

Os quatro pontos antigos mediram ESTADO ao iniciar sessao; nenhum observou a
transicao. Esta sonda observa: uma requisicao por hora, registrando o veredito, e
para sozinha quando ve o bloqueio cair, dizendo quanto durou.

QUANDO RODAR: depois de bater a parede. Bloqueado, o orcamento do dia ja foi e a
requisicao nao custa coleta nenhuma. Rodar com a fonte liberada gasta cota para
confirmar o obvio — 24 requisicoes/dia contra uma parede que fica na casa de 92.

    python sonda-horaria.py              # laco, para na transicao para LIMPO
    python sonda-horaria.py --uma        # uma medicao e sai (para o Agendador do Windows)
    python sonda-horaria.py --intervalo 30   # minutos entre medicoes (padrao 60)

Cada medicao vai para logs/sonda-horaria.csv, que acumula entre execucoes — e a
serie, nao a medicao isolada, que responde a pergunta.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import requests

sys.path.insert(0, "scraper")
import scraperPE as sp   # LOCAL, HEADERS, BASE_URL, RAIO, DATA_DIAS, TZ

ARQUIVO = os.path.join(sp.DIR_LOGS, "sonda-horaria.csv")
CABECALHO = ["quando", "local", "termo", "itens", "ecoados", "ncm_sinteticos", "veredito"]


def medir(sessao: requests.Session, termo: str) -> dict:
    """Uma requisicao. Reporta os dois sinais separados, para a serie ficar auditavel."""
    params = {"termo": termo, "local": sp.LOCAL, "raio": sp.RAIO,
              "data": sp.DATA_DIAS, "ordem": sp.ORDEM}
    agora = datetime.now(sp.TZ)
    try:
        r = sessao.get(f"{sp.BASE_URL}/produtos", headers=sp.HEADERS,
                       params=params, timeout=20)
        r.raise_for_status()
        itens = (r.json() or {}).get("produtos") or []
    except Exception as exc:
        return {"quando": agora, "itens": 0, "ecoados": 0, "ncm_sinteticos": 0,
                "veredito": f"ERRO {exc}"}

    ecoados = sum(1 for i in itens if (i.get("local") or "") == sp.LOCAL)
    sinteticos = sum(1 for i in itens if len(str(i.get("ncm") or "")) == 9)

    if not itens:
        # O gerador de dado falso SEMPRE devolve itens: vazio nao e bloqueio.
        veredito = "SEM ITENS"
    elif sinteticos / len(itens) >= 0.5:
        veredito = "ENVENENADO"
    else:
        veredito = "LIMPO"

    return {"quando": agora, "itens": len(itens), "ecoados": ecoados,
            "ncm_sinteticos": sinteticos, "veredito": veredito}


def registrar(m: dict, termo: str) -> None:
    os.makedirs(sp.DIR_LOGS, exist_ok=True)
    novo = not os.path.exists(ARQUIVO)
    with open(ARQUIVO, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(CABECALHO)
        w.writerow([m["quando"].isoformat(), sp.LOCAL, termo, m["itens"],
                    m["ecoados"], m["ncm_sinteticos"], m["veredito"]])


def linha(m: dict) -> str:
    return (f"{m['quando']:%d/%m %H:%M}  itens={m['itens']:3} "
            f"eco={m['ecoados']:3} ncm9={m['ncm_sinteticos']:3}  ->  {m['veredito']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uma", action="store_true", help="uma medicao e sai")
    ap.add_argument("--intervalo", type=int, default=60, help="minutos entre medicoes")
    ap.add_argument("--termo", default=sp.SONDA_TERMO)
    args = ap.parse_args()

    sessao = requests.Session()
    print(f"sonda | local={sp.LOCAL} termo={args.termo!r} | registro em {ARQUIVO}\n")

    if args.uma:
        m = medir(sessao, args.termo)
        registrar(m, args.termo)
        print(linha(m))
        return

    inicio_bloqueio = None
    while True:
        m = medir(sessao, args.termo)
        registrar(m, args.termo)
        print(linha(m))

        if m["veredito"] == "ENVENENADO" and inicio_bloqueio is None:
            inicio_bloqueio = m["quando"]
            print("     (bloqueio em curso — contando a partir daqui)")

        if m["veredito"] == "LIMPO":
            if inicio_bloqueio is None:
                print("\nJa estava LIMPO. Nao ha transicao para medir, e cada medicao"
                      "\nconsome cota da coleta. Rode esta sonda depois da parede.")
            else:
                horas = (m["quando"] - inicio_bloqueio).total_seconds() / 3600
                print(f"\nTRANSICAO OBSERVADA: bloqueado as {inicio_bloqueio:%d/%m %H:%M},"
                      f" limpo as {m['quando']:%d/%m %H:%M}")
                print(f"  duracao observada de ponta a ponta: {horas:.1f} h"
                      f"  (limite superior — a virada pode ter sido antes desta medicao)")
                print(f"  serie completa em {ARQUIVO}")
            return

        time.sleep(args.intervalo * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrompida — a serie ja registrada continua valendo.")
