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

# Local nao tem o timeout de 30 min do Actions, entao da para ir bem mais devagar.
# 180s = um produto a cada 3 min. Sao 160 produtos, entao a varredura completa leva
# ~8h — mas nao precisa caber numa sessao: o scraper guarda o progresso do dia e
# continua de onde parou na proxima vez que voce rodar.
os.environ.setdefault('SLEEP_REQUESTS', '180')

subprocess.run([sys.executable, 'scraper/scraperPE.py'])
