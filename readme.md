# Clipping DOU – ANTT/SUFER

Sistema automatizado para monitorar publicações do Diário Oficial da União (DOU) relacionadas à **ANTT** (Agência Nacional de Transportes Terrestres) e **SUFER** (Superintendência de Transporte Ferroviário).

## 🎯 Características

- ✅ Busca automatizada 6x por dia
- ✅ Login autenticado no INLABS
- ✅ Apenas Seção 1 (DO1) + edições extras (DO1E)
- ✅ Filtros: órgão específico + palavras-chave
- ✅ E-mail **apenas** quando há achados
- ✅ SQLite para deduplicação e histórico
- ✅ Backfill histórico (sem envio de e-mail)
- ✅ Totalmente gratuito (GitHub Actions)

## 📋 Requisitos

- Conta no [INLABS](https://inlabs.in.gov.br/) (gratuita)
- Conta Gmail com [App Password](https://support.google.com/accounts/answer/185833)
- GitHub repository

## 🚀 Instalação

### 1. Configure os Secrets do GitHub

No seu repositório: **Settings → Secrets and variables → Actions**

Adicione os seguintes secrets:

| Secret | Descrição | Exemplo |
|--------|-----------|---------|
| `INLABS_EMAIL` | E-mail do INLABS | `seu-email@gmail.com` |
| `INLABS_PASSWORD` | Senha do INLABS | `SuaSenha123` |
| `SMTP_USER` | E-mail Gmail remetente | `seu-email@gmail.com` |
| `SMTP_PASS` | App Password do Gmail | `xxxx xxxx xxxx xxxx` |

### 2. Atualize o `config.yml`

Edite o arquivo `config.yml` com suas informações:

```yaml
inlabs:
  email: ""  # Pode deixar vazio (vem do Secret)
  password: ""  # Pode deixar vazio (vem do Secret)

mail:
  from_email: "seu-email@gmail.com"
  to_emails:
    - "destinatario@example.com"
```

### 3. Faça commit e push

```bash
git add .
git commit -m "Setup: Configuração inicial"
git push
```

## ⚙️ Uso

### Execução Automática

O sistema roda **automaticamente 6x por dia**:
- 07:08 BRT
- 10:08 BRT
- 13:08 BRT
- 16:08 BRT
- 19:08 BRT
- 22:08 BRT

### Execução Manual

Vá em **Actions → Clipping Diário → Run workflow**

### Backfill Histórico

1. Vá em **Actions → Backfill Histórico**
2. Clique em **Run workflow**
3. Defina data inicial e final
4. Execute

**Nota:** Backfill **não envia e-mails**, apenas popula o banco de dados.

## 🗄️ Estrutura

```
dou-antt-clipping/
├── clip.py                 # Script principal (~280 linhas)
├── config.yml              # Configuração
├── requirements.txt        # Dependências (3 libs)
├── .github/workflows/
│   ├── daily.yml          # Cron 6x/dia
│   └── backfill.yml       # Backfill manual
└── README.md

# Branch 'data' (criado automaticamente):
└── data/
    └── clipping.sqlite    # Banco persistente
```

## 📊 Banco de Dados

O SQLite contém 3 tabelas:

### `runs`
Registra todas as execuções (com ou sem achados)

### `processed_files`
Arquivos já processados (evita duplicação)

### `matches`
Publicações encontradas com texto completo

## 🧪 Teste Local

```bash
# Instalar dependências
pip install -r requirements.txt

# Executar para hoje
python clip.py --config config.yml run

# Executar sem enviar e-mail
python clip.py --config config.yml run --no-email

# Backfill de um período
python clip.py --config config.yml backfill --start 2024-01-01 --end 2024-12-31
```

## 📧 Formato do E-mail

Quando há achados, você recebe:
- **Assunto:** `[DOU][ANTT][SUFER] 2025-01-07 — 2 achado(s)`
- **Corpo:** HTML formatado com:
  - Palavra-chave que gerou o match
  - Arquivo fonte
  - Trecho do texto (500 caracteres ao redor da palavra-chave)

## 🔧 Configuração Avançada

### Alterar palavras-chave

Edite `config.yml`:

```yaml
filters:
  keywords_any:
    - "sua palavra-chave 1"
    - "sua palavra-chave 2"
```

### Alterar horários de execução

Edite `.github/workflows/daily.yml`:

```yaml
schedule:
  - cron: "0 12 * * *"   # Meio-dia UTC (09:00 BRT)
```

## 💰 Custos

**ZERO** – GitHub Actions oferece 2.000 minutos/mês gratuitamente.

Estimativa:
- 6 exec/dia × 30 dias × 2 min/exec = **360 min/mês** ✅

## 🐛 Troubleshooting

### E-mail não chegou

1. Verifique se há achados: **Actions → Logs → "Run clipping"**
2. Confirme que `SMTP_PASS` é um **App Password** (não senha normal)
3. Verifique spam/lixo eletrônico

### Workflow falhou

1. Veja os logs em **Actions**
2. Verifique se todos os **Secrets** estão configurados
3. Confirme credenciais do INLABS

### Banco de dados corrompeu

O banco fica no branch `data`. Para resetar:

```bash
git push origin --delete data
```

Na próxima execução, será criado novamente.

## 📜 Licença

Uso pessoal/institucional.

## 🤝 Contribuições

Issues e Pull Requests são bem-vindos!

---

**Desenvolvido com ❤️ para monitoramento eficiente do DOU**
