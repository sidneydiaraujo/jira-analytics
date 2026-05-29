# jira-analytics

Skill do Claude Code que analisa dados do Jira em linguagem natural. Responde perguntas sobre sprints, responsáveis, épicos, histórias e muito mais — **somente leitura, nenhum dado é alterado**.

**Projetos monitorados:** TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP, TVAR

---

## O que essa skill faz

- **Relatório de sprint** — taxa de conclusão, story points, histórias por status
- **Risco de atraso** — classifica histórias como CRITICO / ALTO / MEDIO / BAIXO com base em estimativas e subtarefas
- **Métricas por desenvolvedor** — taxa de entrega, MAPE, confiança (ALTA/MEDIA/BAIXA/CRITICA), tendência ao longo de sprints
- **Saúde dos épicos** — detecta campos faltando, garantia vencida, épicos sem responsável
- **Pesquisa de conteúdo** — busca em linguagem natural dentro de descrições, critérios de aceite, cenários de teste e comentários

---

## Pré-requisitos

- [Claude Code](https://claude.ai/code) instalado
- Python 3.8 ou superior
- Biblioteca `requests`: `pip install requests`
- Conta Atlassian com acesso ao Jira (`qx3prod.atlassian.net`)
- Token de API do Jira ([gerar aqui](https://id.atlassian.com/manage-profile/security/api-tokens))

---

## Instalação

### 1. Clone o repositório na pasta de skills do Claude

**macOS / Linux:**
```bash
mkdir -p ~/.claude/skills
cd ~/.claude/skills
git clone https://github.com/sidneydiaraujo/jira-analytics
```

**Windows (PowerShell):**
```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills"
cd "$env:USERPROFILE\.claude\skills"
git clone https://github.com/sidneydiaraujo/jira-analytics
```

### 2. Configure as variáveis de ambiente

Abra (ou crie) o arquivo `~/.claude/settings.json` e adicione:

```json
{
  "env": {
    "JIRA_EMAIL": "seu-email@empresa.com",
    "JIRA_API_TOKEN": "seu-token-de-api-aqui"
  }
}
```

> **Como gerar o token:** acesse https://id.atlassian.com/manage-profile/security/api-tokens → "Create API token"

### 3. Instale as dependências Python

```bash
pip install requests
```

### 4. Reinicie o Claude Code

Feche e reabra o Claude Code para carregar a skill.

---

## Como usar

Basta perguntar naturalmente no chat do Claude Code:

```
"Como está o sprint atual do TPROJ?"
"Quais histórias têm risco de atraso?"
"Quem está mais sobrecarregado no time?"
"Qual a confiança de entrega dos devs nos últimos 5 sprints?"
"Quais épicos estão em risco?"
"Qual o progresso do épico TPROJ-123?"
"Tem algum critério de aceite para o campo CCEE?"
"O que foi combinado sobre a fatura de venda?"
"Busca cenário de teste para migração de ativo"
```

---

## Exemplos de perguntas

| Pergunta | O que retorna |
|---|---|
| "Como está o sprint atual do TPROJ?" | Total de histórias, taxa de conclusão, story points |
| "Histórias em andamento com risco" | Lista ordenada por criticidade com tempo no status |
| "Métricas dos devs nos últimos 6 sprints" | Tabela com taxa de entrega, MAPE, confiança por desenvolvedor |
| "Épicos sem responsável" | Lista de épicos com campos faltando |
| "O que foi decidido sobre garantia?" | Trechos de comentários e descrições com contexto |

---

## Estrutura do projeto

```
jira-analytics/
├── SKILL.md              # Definição da skill (lida pelo Claude)
├── scripts/
│   └── analyzer.py       # Todos os módulos de análise
└── README.md
```

---

## Segurança

- As credenciais ficam em `~/.claude/settings.json` — **nunca** as adicione ao repositório
- Essa skill é estritamente de leitura — nenhuma chamada de escrita ou transição é feita
