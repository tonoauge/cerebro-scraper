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

## Manutenção — os 60 dias do Actions

O GitHub **desliga os workflows agendados** quando o repositório passa **60 dias sem
nenhum commit**. Como aqui só se mexe quando um scraper quebra, é normal ficar meses
parado com a coleta rodando sozinha — e aí o agendamento cai por inatividade, sem
nada ter dado errado no código.

Isso é automático desde 12/11/2026: [`keepalive.yml`](.github/workflows/keepalive.yml)
roda toda segunda, mede quantos dias se passaram desde o último commit e **só** move o
repositório (carimbo em `.github/keepalive`) quando passa de **45 dias**. Enquanto
houver trabalho real no repo, ele não commita nada. Nenhum workflow tem gatilho `push`,
então esse commit não dispara coleta.

Se o agendamento já tiver sido desligado, um commit **não** o religa — é preciso
reativar cada workflow na aba Actions, ou `gh workflow enable <id>`. Vale lembrar que o
próprio keepalive é agendado: se ele for desligado junto, religar os workflows é o passo
que volta a proteger o resto.
