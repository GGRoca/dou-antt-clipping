# Clipping DOU – ANTT/SUFER

Monitoramento automático do Diário Oficial da União (Seção 1) para publicações da
**ANTT** (Agência Nacional de Transportes Terrestres) e da **SUFER** (Superintendência
de Transporte Ferroviário), via [INLABS](https://inlabs.in.gov.br/), rodando de graça no
GitHub Actions.

## Como funciona

1. Login no INLABS pelo protocolo oficial (`POST logar.php` → cookie `inlabs_session_cookie`).
2. Para cada dia da janela (D-5 ... D0) lista os arquivos publicados e baixa os ZIPs
   ainda não processados (`AAAA-MM-DD-DO1.zip`, `-DO1E.zip`, `-DO1A.zip`...). ZIPs de
   edição extra já vistos são re-baixados e reprocessados se o tamanho mudou.
3. Cada XML do ZIP é um `<article>` com os metadados nos atributos (`artType`,
   `artCategory`, `pubDate`, `editionNumber`, `numberPage`, `pdfPage`) e o conteúdo em
   CDATA dentro de `<body>` (`Identifica`, `Ementa`, `Texto`...).
4. Os filtros do `config.yml` casam **órgão** (substring de `artCategory`) e **tipo de
   ato** (`artType`). Anexos/quadros sem `Identifica` são ignorados.
5. Publicações novas (chave = `id` do article) entram no SQLite e vão por e-mail com o
   texto completo formatado como no DOU e link para a página.
6. Edições extras publicadas **só em PDF** (comum em fins de semana): se o PDF menciona
   um dos órgãos, sai um alerta para conferência manual — o ato não é recortado do PDF.

O banco (`data/clipping.sqlite`) vive no branch `data` e é restaurado/persistido a cada
execução, inclusive quando a execução falha.

## Política de e-mail

| Situação | E-mail |
|---|---|
| Publicação nova encontrada | Imediato, em qualquer execução (até 25 por mensagem) |
| Dia útil sem novidades | 1 e-mail "sem novidades" na primeira execução bem-sucedida do dia (BRT) depois que a edição do dia foi processada (ou após 12h, se não houver edição) |
| INLABS inacessível há mais de 24h | Alerta (no máximo 1 por dia) |
| SMTP falhou | Execução marcada como erro (fica vermelha); os achados ficam pendentes e são reenviados na próxima |
| INLABS fora do ar em uma execução | Sai com sucesso (exit 0) e registra `inlabs_unavailable` no banco; tenta de novo na próxima |

## Configuração

Secrets do repositório (**Settings → Secrets and variables → Actions**):

| Secret | Conteúdo |
|---|---|
| `INLABS_EMAIL` / `INLABS_PASSWORD` | Conta do INLABS |
| `SMTP_USER` / `SMTP_PASS` | Gmail remetente e [App Password](https://support.google.com/accounts/answer/185833) |

Filtros, destinatários e janela ficam em `config.yml` (comentado). Para validar os
filtros contra dados reais: **Actions → Clipping Diário → Run workflow → comando
`inspect`** — o log mostra todos os `artCategory`/`artType` do dia que contêm
"Transportes" e o que cada filtro capturaria.

## Comandos

```bash
pip install -r requirements.txt
python clip.py run                        # janela D-5..D0, envia e-mail
python clip.py run --date 2026-09-15 --no-email --force
python clip.py inspect --date 2026-09-15  # diagnóstico dos filtros
python clip.py backfill --start 2026-01-01 --end 2026-01-31   # sem e-mail
python clip.py test-email                 # valida SMTP
python -m pytest -q                       # testes (servidor INLABS simulado)
```

Localmente, exporte `INLABS_EMAIL`, `INLABS_PASSWORD`, `SMTP_USER`, `SMTP_PASS`.

## Workflows

- **Clipping Diário** (`daily.yml`): 6x por dia (07:08, 10:08, 13:08, 16:08, 19:08 e
  22:08 BRT, sujeitos ao atraso do GitHub) e manual, com os comandos `run`, `inspect`
  e `test-email`.
- **Backfill Histórico** (`backfill.yml`): popula o banco em um intervalo de datas sem
  enviar e-mail (os achados ficam marcados como `backfill`).

## Banco de dados

- `runs` — uma linha por execução: `status` (`ok`, `inlabs_unavailable`, `error`), arquivos, novos matches, e-mails.
- `processed_files` — arquivos já baixados (não são baixados de novo).
- `matches` — publicações encontradas, com texto e HTML; `emailed_ts` nulo = pendente de envio.
- `state` — `last_success_ts`, `last_email_date`, `unavailable_alert_ts`.

Bancos da versão anterior são migrados automaticamente (tabelas antigas viram `legacy_*`).

## Estrutura

```
clip.py                     # todo o sistema
config.yml                  # filtros, e-mail, janela
tests/test_clip.py          # parser, filtros, banco, e-mail, cliente (servidor falso)
scripts/restore_db.sh       # branch data -> data/clipping.sqlite
scripts/persist_db.sh       # data/clipping.sqlite -> branch data
.github/workflows/          # daily.yml, backfill.yml
```
