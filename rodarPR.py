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

subprocess.run([sys.executable, 'scraper/scraperPE.py'])
