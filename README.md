# cerebro-scraper

Runner de **scraping de preços** do app agrodados (insumos e frutas do Vale do São
Francisco). Repositório público e dedicado apenas para rodar os scrapers no GitHub
Actions — o app Next.js, os endpoints `/api/cron/*` e o painel admin ficam no
repositório privado.

## O que roda aqui

**Workers** (`.github/workflows/`, disparados por `workflow_dispatch`, em lotes):

| Workflow | Script | Fonte | Região |
|----------|--------|-------|--------|
| `scraper.yml` | `scraper/scraper.py` | Preço da Hora BA (insumos) | BA-Juazeiro |
| `scraperPE.yml` | `scraper/scraperPE.py` | Menor Preço PR (insumos) | Petrolina |
| `scraperFrutasBA.yml` | `scraper/scraperFrutasBA.py` | Preço da Hora BA (frutas) | BA-Juazeiro |
| `scraperFrutasPE.yml` | `scraper/scraperFrutasPE.py` | Menor Preço PR (frutas) | Petrolina |

**Roteadores** (agendados via `schedule`): consultam a API do app
(`$AGRODADOS_URL/api/cron/verificar-grupo` e `.../verificar-agenda`) para saber quais
lotes disparar e fazem `gh workflow run` dos workers deste mesmo repositório.

- `scraper-cron.yml` — legado (BA/PE ter-sex, cfru-ba diário)
- `scraper-cron-frutaspe.yml` — agenda (cfru-pe)

## Secrets necessários (Settings → Secrets and variables → Actions)

`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `CRON_SECRET`, `AGRODADOS_URL`, `GH_PAT` (scope
`workflow`). Nenhum segredo é impresso nos logs — os scrapers leem tudo de variáveis de
ambiente.

## Documentação

A referência de funcionamento (fluxo, lotes, agenda, incidentes) vive no repositório
privado do app em `docs/scraping-precos.md`.
