import os, subprocess, sys

# Frutas PE — fonte "cfru-pe" na cron_config (site: menorpreco.notaparana.pr.gov.br).
# Sem GRUPO_INICIO/GRUPO_FIM ele roda em modo MANUAL: todos os produtos ativos.

with open('.env.local') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

# Local nao tem o timeout de 30 min do Actions, entao da para ir bem mais devagar.
# 180s entre cada consulta. Aqui a pausa ocorre 4x por produto (uma por ponto de coleta
# + uma no fim), ou seja 12 min por produto: 16 produtos = ~3h12 a varredura completa.
# O progresso do dia fica salvo, entao da para rodar em mais de uma sessao.
os.environ.setdefault('SLEEP_REQUESTS', '180')

subprocess.run([sys.executable, 'scraper/scraperFrutasPE.py'])
