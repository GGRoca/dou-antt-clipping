# Clipping DOU – ANTT / SUFER (INLABS)

Este projeto implementa um **clipping automatizado do Diário Oficial da União (DOU)**,
com foco em publicações da:

**Ministério dos Transportes → ANTT → Superintendência de Transporte Ferroviário (SUFER)**

usando como fonte o **INLABS (Imprensa Nacional – dados abertos)**.

---

## 🎯 Objetivo

- Identificar automaticamente publicações do DOU que:
  - sejam da **ANTT / SUFER**, e
  - contenham os termos:
    - **“outorga por autorização ferroviária”**, ou
    - **“autorização”** (desde que atendam ao critério de órgão)
- Cobrir:
  - edições **normais**
  - edições **extras / suplementares**
- Rodar **6 vezes por dia**, garantindo cobertura mesmo quando extras saem fora do horário padrão
- Armazenar **todas as ocorrências e execuções** em banco local
- Enviar **e-mail apenas quando houver achados**
- Manter histórico auditável (inclusive dias sem publicação)

---

## 🧠 Arquitetura

dou-antt-clipping/
├─ douclip/ # código do projeto
├─ data/
│ └─ douclip.sqlite # banco SQLite (criado automaticamente)
├─ config.yml # configuração geral
├─ requirements.txt
└─ .github/workflows/
└─ daily.yml # GitHub Actions (cron)



---

## ⏱️ Agendamento (GitHub Actions)

O workflow roda **6 vezes por dia**, nos horários (BRT):

- 07:08
- 10:08
- 13:08
- 16:08
- 19:08
- 22:08

Isso garante que **edições extras tardias** sejam capturadas sem depender de horário fixo do DOU.

---

## 🗄️ Banco de dados

O banco `data/douclip.sqlite` é criado automaticamente.

Tabelas principais:

- `runs`  
  Registra **todas as execuções**, inclusive quando não há achados.

- `processed_files`  
  Lista todos os arquivos do INLABS já processados (evita duplicação).

- `matches`  
  Armazena o **texto completo** das publicações relevantes encontradas.

---

## 📧 Envio de e-mail

- O e-mail **só é enviado quando há achados**
- Conteúdo:
  - texto completo da publicação
  - link para o arquivo no INLABS / DOU
- Envio via **SMTP (Gmail com App Password)**

As credenciais **NÃO ficam no código** — são definidas via **GitHub Actions Secrets**.

---

## 🔐 Secrets necessários (GitHub)

No repositório → `Settings → Secrets and variables → Actions`:

| Nome        | Valor                                  |
|-------------|----------------------------------------|
| SMTP_USER   | e-mail remetente (ex.: Gmail)          |
| SMTP_PASS   | senha de app (App Password do Gmail)   |

---

## ▶️ Execução manual (local)

```bash
pip install -r requirements.txt
python -m douclip run --config config.yml
```

## Backfill histórico


python -m douclip backfill --config config.yml --start 2021-12-23 --end 2025-12-31

📌 Observações importantes

O INLABS não tem horário fixo para edições extras.

O projeto resolve isso por:

múltiplas execuções diárias

controle de arquivos já processados

O sistema é idempotente: pode rodar várias vezes sem duplicar dados.

📄 Licença

Uso interno / institucional.

