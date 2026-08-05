import os, subprocess, sys

# Insumos PE — fonte "PE" na cron_config (site: menorpreco.notaparana.pr.gov.br).
# O nome PR vem do site ser do Parana; no repo o scraper se chama scraperPE.py.
# Sem GRUPO_INICIO/GRUPO_FIM ele roda em modo MANUAL: todos os produtos ativos.

with open('.env.local') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

# A fonte tolera ~25 produtos por sessao e depois envenena, independente do ritmo -
# o limite e de volume, nao de velocidade. Mas o contador decai com o tempo, entao a
# estrategia e trabalhar em blocos e descansar entre eles.
#
#   20 produtos x 60s = 20 min de trabalho
#   + 60 min de descanso  = 80 min por ciclo, ~20 produtos por ciclo
#
# Nao precisa caber numa sessao: o progresso fica salvo e a proxima execucao continua
# pelos produtos mais desatualizados. Se a fonte envenenar antes do fim do bloco, ele
# descansa e tenta de novo; so desiste apos MAX_ENVENENAMENTOS seguidos.
os.environ.setdefault('SLEEP_REQUESTS', '60')
os.environ.setdefault('BLOCO', '20')
os.environ.setdefault('DESCANSO_MIN', '60')

subprocess.run([sys.executable, 'scraper/scraperPE.py'])
