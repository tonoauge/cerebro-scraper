import os, subprocess, sys

# Frutas PE — fonte "cfru-pe" na cron_config (site: menorpreco.notaparana.pr.gov.br).
# Sem GRUPO_INICIO/GRUPO_FIM ele roda em modo MANUAL: todos os produtos ativos.

with open('.env.local') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

# Aqui a pausa ocorre 4x por produto (uma por ponto de coleta + uma no fim), entao 60s
# ja da 4 min por produto: os 16 produtos levam ~1h04. Como sao poucos, o bloco de 20
# nunca e atingido e o descanso so entra se a fonte envenenar no meio - ai ele recua,
# espera e retoma o mesmo produto. O progresso fica salvo entre sessoes.
os.environ.setdefault('SLEEP_REQUESTS', '60')
os.environ.setdefault('BLOCO', '20')
os.environ.setdefault('DESCANSO_MIN', '60')

subprocess.run([sys.executable, 'scraper/scraperFrutasPE.py'])
