# 📋 Guia de Implementação - Sistema Final

## 🎯 O Que Mudou (Versão Final)

### ✅ **1. E-mail Inteligente**
- **Segunda a Sexta, ~10:08 BRT**: SEMPRE envia e-mail
  - Com achados: "2 achado(s) encontrado(s)"
  - Sem achados: "Sistema operacional (0 achados)"
- **Outros horários + Fim de semana**: Só envia se houver achados
- **Lógica**: Detecta automaticamente horário e dia da semana

### ✅ **2. Janela de Revarredura (D-2, D-1, D+0)**
- Cada execução busca **últimos 3 dias**
- **Por quê**: Edições extras podem sair com delay
- **Deduplicação**: SQLite garante que não processa o mesmo arquivo 2x
- **Configurável**: `lookback_days: 2` no config.yml

### ✅ **3. PDF Fallback**
- **Prioridade**: ZIP (contém XMLs estruturados)
- **Fallback**: Se não houver ZIP, processa PDF
- **Cobertura**: DO1.zip, DO1E.zip + todos PDFs extras (A, B, C)
- **Parser**: PyPDF2 (adiciona~5MB mas é necessário)

### ✅ **4. Arquitetura Multi-Filtro (Extensível)**
```yaml
filtros:
  - nome: "ANTT-SUFER-Autorizacoes"
    secao: "DO1"
    orgao: "ANTT/SUFER"
    keywords: ["autorização"]
  
  # Adicionar novo filtro: só descomenta e edita!
  # - nome: "MinTransportes-Ministro"
  #   secao: "DO1"
  #   orgao: "Ministério dos Transportes"
  #   keywords: ["Renan Filho"]
```

**Como adicionar filtro novo:**
1. Descomenta as linhas no `config.yml`
2. Edita nome, seção, órgão, keywords
3. Commit + push
4. **PRONTO!** Próxima execução já usa o novo filtro

---

## 📦 Arquivos Gerados

### **Para substituir:**
1. `clip_v2.py` → renomear para `clip.py`
2. `config_v2.yml` → renomear para `config.yml`
3. `requirements_v2.txt` → renomear para `requirements.txt`

### **Manter:**
- `.github/workflows/daily.yml` (já corrigido)
- `.github/workflows/backfill.yml` (já corrigido)
- `README.md` (atualizar depois)
- `.gitignore`

---

## 🚀 Passos de Implementação

### **1. Substitua os arquivos**

```powershell
# No seu repositório local

# Renomeia os arquivos baixados
Move-Item clip_v2.py clip.py -Force
Move-Item config_v2.yml config.yml -Force
Move-Item requirements_v2.txt requirements.txt -Force
```

### **2. Edite config.yml**

Preencha seus e-mails:

```yaml
mail:
  from_email: "guilherme.artintel@gmail.com"
  to_emails:
    - "guiroca@gmail.com"
```

### **3. Commit e Push**

```powershell
git add clip.py config.yml requirements.txt
git commit -m "v2: E-mail inteligente + PDF fallback + Multi-filtro + Lookback D-2"
git push
```

### **4. Teste no GitHub Actions**

**Actions → Clipping Diário → Run workflow**

Aguarde ~2-3 minutos e verifique:
- ✅ Login INLABS
- ✅ Busca 3 dias (D-2, D-1, D+0)
- ✅ Processa ZIPs e PDFs
- ✅ Persiste banco no branch `data`

---

## 📧 Comportamento do E-mail

### **Cenário 1: Segunda-Feira, 10:08 BRT, SEM achados**
```
Para: guiroca@gmail.com
Assunto: [DOU][ANTT][SUFER] 2026-01-07 — Sistema operacional (0 achados)

✓ Sistema operacional
Nenhuma publicação encontrada com os critérios de busca.

Este é um e-mail de confirmação diária (segunda a sexta, 10:08 BRT).
O sistema continua monitorando o DOU automaticamente.
```

### **Cenário 2: Terça-Feira, 16:08 BRT, COM achados**
```
Para: guiroca@gmail.com
Assunto: [DOU][ANTT][SUFER] 2026-01-07 — 2 achado(s)

✅ Total de achados: 2

─────────────────────
Achado #1 — Filtro: ANTT-SUFER-Autorizacoes
Palavra-chave: autorização
Arquivo fonte: 2026-01-07-DO1.zip

[Snippet do texto...]
─────────────────────
```

### **Cenário 3: Sábado, 13:08 BRT, SEM achados**
```
(Nenhum e-mail enviado)
```

### **Cenário 4: Domingo, 13:08 BRT, COM achados**
```
Para: guiroca@gmail.com
Assunto: [DOU][ANTT][SUFER] 2026-01-08 — 1 achado(s)

✅ Total de achados: 1
[...]
```

---

## 🔧 Como Adicionar Novo Filtro (Exemplo Real)

### **Cenário: Quer monitorar menções ao "Ministro Renan Filho"**

**1. Edite `config.yml`:**

```yaml
filtros:
  # Mantém o existente
  - nome: "ANTT-SUFER-Autorizacoes"
    secao: "DO1"
    orgao: "Ministério dos Transportes/Agência Nacional de Transportes Terrestres/Superintendência de Transporte Ferroviário"
    keywords:
      - "outorga por autorização ferroviária"
      - "autorização"
  
  # ADICIONA NOVO:
  - nome: "MinTransportes-Ministro"
    secao: "DO1"
    orgao: "Ministério dos Transportes"
    keywords:
      - "Renan Filho"
      - "Ministro de Estado dos Transportes"
```

**2. Commit + Push:**

```powershell
git add config.yml
git commit -m "Add: Filtro para Ministro Renan Filho"
git push
```

**3. Pronto!**

Na próxima execução (ou execute manualmente), o sistema:
- ✅ Busca publicações com "Ministério dos Transportes"
- ✅ Filtra por "Renan Filho" OU "Ministro de Estado dos Transportes"
- ✅ Inclui no e-mail: "Achado #X — Filtro: MinTransportes-Ministro"

---

## 📊 Banco de Dados (Atualizado)

### **Tabela `matches` - MUDOU:**

```sql
CREATE TABLE matches (
    id INTEGER PRIMARY KEY,
    run_date TEXT,
    filter_name TEXT,  -- NOVO! Identifica qual filtro gerou o match
    source_file TEXT,
    keyword_hit TEXT,
    text_snippet TEXT,
    created_ts TEXT
);
```

**Exemplo de query:**

```sql
-- Quantos achados por filtro?
SELECT filter_name, COUNT(*) as total
FROM matches
GROUP BY filter_name
ORDER BY total DESC;

-- Achados do último mês
SELECT * FROM matches
WHERE created_ts >= date('now', '-30 days')
ORDER BY created_ts DESC;
```

---

## ⚠️ Pontos de Atenção

### **1. PyPDF2 (+5MB)**
- Necessário para processar PDFs
- Se tiver problema de espaço, pode remover mas perde cobertura de extras

### **2. Janela de 3 dias**
- A cada execução, processa D-2, D-1, D+0
- **Vantagem**: Captura extras tardias
- **Desvantagem**: +20-30seg por execução
- **Ajustar**: Mude `lookback_days: 1` no config (só D-1, D+0)

### **3. E-mail diário (10:08)**
- Baseado em **hora UTC** do servidor GitHub
- Aproximação: 09:00-11:00 UTC = ~10:08 BRT
- Se quiser ajustar, edite função `should_always_send_email()`

### **4. Múltiplos filtros**
- Cada filtro adicional = +alguns segundos
- Todos aparecem no mesmo e-mail consolidado
- Banco diferencia por `filter_name`

---

## 🧪 Testes Recomendados

### **Teste 1: Execução manual (hoje)**
```
Actions → Clipping Diário → Run workflow
```
**Verifica:**
- ✅ Login funciona
- ✅ Busca 3 dias
- ✅ Processa ZIPs e PDFs
- ✅ E-mail chega (se for seg-sex 10:08)

### **Teste 2: Backfill pequeno**
```
Actions → Backfill Histórico → Run workflow
Período: 2025-12-20 a 2025-12-27 (1 semana)
```
**Verifica:**
- ✅ Processa múltiplos dias
- ✅ Sem envio de e-mail
- ✅ Banco cresce corretamente

### **Teste 3: Adicionar filtro novo**
1. Adiciona filtro teste no config
2. Commit + push
3. Executa manual
4. Verifica e-mail mostra `filter_name` correto

---

## 📈 Próximos Passos (Opcionais)

### **Melhoria 1: Dashboard**
- Criar script Python que lê SQLite
- Gera gráfico: achados por dia/semana/mês
- Roda no GitHub Actions 1x por semana

### **Melhoria 2: Webhook Slack/Discord**
- Além de e-mail, envia para Slack
- Útil para equipes

### **Melhoria 3: Filtros com regex**
- Em vez de keywords simples, aceita regex
- Ex: `"autorização n[oº] \d+"`

---

## 🆘 Troubleshooting

### **E-mail não chegou (seg-sex 10:08)**
1. Verifique horário UTC do servidor: logs mostram quando rodou
2. Confirme `should_always_send_email()` retorna True
3. Verifique spam/lixeira

### **PDF não foi processado**
1. Confirme PyPDF2 instalado: `pip list | grep PyPDF2`
2. Verifique se ZIP correspondente existe (ZIP tem prioridade)
3. Veja logs: "Erro processando X.pdf: ..."

### **Filtro novo não funciona**
1. Confira indentação YAML (espaços, não tabs!)
2. Teste localmente: `python clip.py --config config.yml run`
3. Veja logs: deve mostrar "Filtro: [nome]"

---

## ✅ Checklist Final

Antes de marcar como "pronto":

- [ ] Arquivos substituídos (clip.py, config.yml, requirements.txt)
- [ ] config.yml com seus e-mails corretos
- [ ] GitHub Secrets configurados (4 secrets)
- [ ] Commit + push realizado
- [ ] Teste manual executado e bem-sucedido
- [ ] Branch `data` criado com banco SQLite
- [ ] E-mail recebido (se rodou seg-sex 10:08) ou confirmado nos logs
- [ ] Backfill pequeno (1 semana) executado para validar
- [ ] Documentação lida e compreendida

---

**Sistema está COMPLETO e PRONTO para produção!** 🎉

Qualquer dúvida ou ajuste, é só pedir!
