# jira-analytics

Skill do Claude Code que analisa dados do Jira em linguagem natural. Responde perguntas sobre sprints, responsáveis, épicos, histórias e muito mais — **somente leitura, nenhum dado é alterado**.

**Projetos monitorados:** TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP, TVAR

---

## O que essa skill faz

- **Relatório de sprint** — taxa de conclusão, story points, histórias por status
- **Risco de atraso** — classifica histórias como CRITICO / ALTO / MEDIO / BAIXO com base em estimativas e tempo no status
- **Métricas por desenvolvedor** — taxa de entrega, MAPE, confiança (ALTA/MEDIA/BAIXA/CRITICA) ao longo de sprints
- **Saúde dos épicos** — detecta campos faltando, garantia vencida, épicos sem responsável
- **Pesquisa de conteúdo** — busca em linguagem natural dentro de descrições, critérios de aceite, cenários de teste e comentários

---

## Pré-requisitos

- [Claude Code](https://claude.ai/code) instalado (desktop, CLI ou extensão de IDE)
- Python 3.8 ou superior — [baixar em python.org](https://python.org) se não tiver
- Conta Atlassian com acesso ao Jira (`qx3prod.atlassian.net`)
- Token de API do Jira — [gerar aqui](https://id.atlassian.com/manage-profile/security/api-tokens)

> A biblioteca `requests` é instalada automaticamente na primeira execução.

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

Localize o arquivo `settings.json` do Claude Code:

| Sistema | Caminho |
|---|---|
| Windows | `C:\Users\<seu-usuario>\.claude\settings.json` |
| macOS / Linux | `~/.claude/settings.json` |

**Se o arquivo não existir**, crie-o com este conteúdo:

```json
{
  "env": {
    "JIRA_EMAIL": "seu-email@thunders.com.br",
    "JIRA_API_TOKEN": "seu-token-de-api-aqui"
  }
}
```

**Se o arquivo já existir** (você já tem outras skills ou configurações), **adicione apenas o bloco `env`** sem apagar o restante:

```json
{
  "model": "sonnet",
  "env": {
    "JIRA_EMAIL": "seu-email@thunders.com.br",
    "JIRA_API_TOKEN": "seu-token-de-api-aqui"
  }
}
```

> ⚠️ Nunca substitua o arquivo inteiro — isso apaga configurações existentes de outras skills.

### 3. Reinicie o Claude Code

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
│   ├── analyzer.py       # Todos os módulos de análise
│   └── config_manager.py # Perfil persistente do usuário (boards, devs, assuntos)
└── README.md
```

---

## Integração com outras skills Jira

| Skill | Para que serve |
|---|---|
| `jira-analytics` | Análise de sprints, métricas de time, saúde de épicos — somente leitura |
| [`jira-epic-automator`](https://github.com/sidneydiaraujo/jira-epic-automator) | Ciclo de vida de épicos: Quarter, datas, garantia, status |
| [`jira-dev`](https://github.com/sidneydiaraujo/jira-dev) | Criação e documentação de tickets do dia a dia do time |

---

## Segurança

- As credenciais ficam em `settings.json` local — **nunca** as adicione ao repositório
- Esta skill é estritamente de leitura — nenhuma chamada de escrita ou transição é feita
