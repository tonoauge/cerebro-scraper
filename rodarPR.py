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

# Quanto a fonte tolera por sessao NAO e fixo. Medido em 10 sessoes locais entre 05 e
# 27/08/2026, em requisicoes ate o envenenamento:
#
#   40, 40, 78, 80, 80, 104, 112, 200, 200, 224   (mediana 92)
#
# Nao ha teto estavel, e o ritmo nao explica: 30s entre produtos deu 27, 180s deu 23.
# O progresso fica salvo, entao nao precisa caber numa sessao — a proxima continua
# pelos produtos mais desatualizados e o catalogo inteiro e coberto ao longo de varias.
#
#   20 produtos x 60s = 20 min de trabalho + 60 min de descanso = ~20 produtos por ciclo
os.environ.setdefault('SLEEP_REQUESTS', '60')
os.environ.setdefault('BLOCO', '20')
os.environ.setdefault('DESCANSO_MIN', '60')
# Encerrar na primeira parede. As tentativas seguintes nunca recuperaram nada nas 10
# sessoes medidas e custavam ~2 h cada. Ver scraper/scraperPE.py.
os.environ.setdefault('MAX_ENVENENAMENTOS', '1')

subprocess.run([sys.executable, 'scraper/scraperPE.py'])
