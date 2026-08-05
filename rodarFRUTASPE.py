import os, subprocess, sys

# Frutas PE — fonte "cfru-pe" na cron_config (site: menorpreco.notaparana.pr.gov.br).
# Sem GRUPO_INICIO/GRUPO_FIM ele roda em modo MANUAL: todos os produtos ativos.

with open('.env.local') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

subprocess.run([sys.executable, 'scraper/scraperFrutasPE.py'])
