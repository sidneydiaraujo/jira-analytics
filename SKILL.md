---
name: jira-analytics
description: "Analise inteligente de dados do Jira para os projetos TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP e TVAR. Use esta skill sempre que o usuario quiser entender, analisar ou consultar dados do Jira — sem alterar nada. Acione para perguntas como: 'como esta o sprint?', 'quem esta sobrecarregado?', 'qual a velocidade do time?', 'quais epicos estao em risco?', 'mostre o progresso do TPROJ-123', 'quem entregou mais historias?', 'qual sprint foi o melhor?', 'tem algum epico sem responsavel?', 'qual a taxa de conclusao?', ou qualquer consulta analitica sobre sprints, responsaveis, epicos ou issues no Jira. Esta skill e somente leitura — nao altera nenhum dado."
---

# Jira Analytics

Analisa dados do Jira (`qx3prod.atlassian.net`) respondendo perguntas sobre sprints, responsáveis, épicos e qualquer consulta via JQL. **Somente leitura — nenhum dado é alterado.**

**Projetos monitorados:** TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP, TVAR

## Configuração

Reutiliza as mesmas variáveis da `jira-epic-automator`:
```
JIRA_EMAIL=<seu-email-atlassian>
JIRA_API_TOKEN=<seu-token-de-api>
```

---

## Módulos de Análise

### Módulo 1 — Sprints

**Gatilhos:** "como está o sprint", "velocidade do time", "taxa de conclusão", "sprint atual", "historias não concluídas"

#### `get_sprint_report(board_id, sprint_id=None)`
Métricas completas de um sprint:
- Total de histórias, concluídas, em andamento, não iniciadas
- Taxa de conclusão (%)
- Story points totais e concluídos
- Histórias sem responsável
- Lista das não concluídas

Se `sprint_id` não for informado, usa o sprint ativo do board. Se não houver ativo, usa o último fechado.

#### `get_sprint_velocity(board_id, num_sprints=5)`
Velocidade dos últimos N sprints fechados:
- Story points entregues por sprint
- Média de velocidade

---

### Módulo 2 — Responsáveis

**Gatilhos:** "quem está sobrecarregado", "carga do time", "produtividade", "quem entregou mais", "distribuição de trabalho"

#### `get_assignee_workload(project_keys=None)`
Carga atual de todos os responsáveis nos sprints abertos:
- Total de issues, concluídas, em andamento, não iniciadas
- Story points por pessoa
- Ordenado do mais carregado para o menos

#### `get_assignee_productivity(assignee_query, days=30)`
Produtividade individual nos últimos N dias:
- Issues e story points entregues
- Issues e story points em aberto
- Busca o usuário por nome ou email

---

### Módulo 3 — Épicos

**Gatilhos:** "saúde dos épicos", "épicos em risco", "campos faltando", "garantia vencida", "progresso do épico", "epicos sem responsável"

#### `get_epics_health(project_keys=None)`
Diagnóstico de todos os épicos ativos:
- Sem responsável
- Sem Quarter
- Sem Start Date
- Sem Data de Publicação
- Garantia vencida (Data de Garantia < hoje)
- Lista de épicos saudáveis vs em risco

#### `get_epic_progress(epic_key)`
Progresso detalhado de um épico específico:
- Total de histórias e % de conclusão
- Story points concluídos vs total
- Contagem por status

---

### Módulo 4 — Consulta Livre

**Gatilhos:** qualquer pergunta que não se encaixe nos módulos acima, ou quando o usuário quer buscar por critérios específicos

#### `free_query(jql, fields, max_results=50)`
Executa qualquer JQL e retorna resultado estruturado:
- Chave, resumo, status, responsável, prioridade, tipo
- Use quando o usuário fizer perguntas abertas que exigem filtros personalizados

**Exemplos de JQL que a skill monta automaticamente:**
```
# Bugs abertos sem responsável
issuetype = Bug AND assignee is EMPTY AND status != Done

# Histórias atrasadas no sprint
sprint in openSprints() AND duedate < now() AND status != Done

# Épicos sem data de publicação
issuetype = Epic AND cf[11336] is EMPTY AND status not in (Done, Fechado)
```

---

## Como a Skill Responde

A skill analisa a pergunta do usuário, seleciona o módulo adequado, executa as chamadas necessárias e responde diretamente no chat com:

- **Tabelas** para comparações (ex: carga por responsável)
- **Listas** para itens de atenção (ex: épicos em risco)
- **Métricas em destaque** para resumos (ex: taxa de conclusão 78%)
- **Recomendações** quando identificar padrões de risco

---

## Scripts Disponíveis

### `scripts/analyzer.py`
Módulos de análise reutilizáveis:
- `list_boards(project_key)` — lista boards disponíveis
- `get_sprint_report(board_id, sprint_id)` — relatório de sprint
- `get_sprint_velocity(board_id, num_sprints)` — velocidade histórica
- `get_assignee_workload(project_keys)` — carga por responsável
- `get_assignee_productivity(assignee_query, days)` — produtividade individual
- `get_epics_health(project_keys)` — diagnóstico de épicos
- `get_epic_progress(epic_key)` — progresso de um épico
- `free_query(jql, fields, max_results)` — consulta JQL livre

---

## Exemplos de Uso

| O usuário pergunta | Ação |
|---|---|
| "Como está o sprint atual do TPROJ?" | `get_sprint_report` no board do TPROJ |
| "Qual a velocidade do time nos últimos 5 sprints?" | `get_sprint_velocity` |
| "Quem está mais sobrecarregado?" | `get_assignee_workload` — ordena pelo total |
| "Como foi a produtividade do João este mês?" | `get_assignee_productivity("João", 30)` |
| "Quais épicos estão em risco?" | `get_epics_health` — filtra `em_risco` |
| "Qual o progresso do TPROJ-123?" | `get_epic_progress("TPROJ-123")` |
| "Tem bugs abertos sem responsável?" | `free_query` com JQL adequado |
| "Quais histórias estão atrasadas no sprint?" | `free_query` com `duedate < now()` |

---

## Tratamento de Erros

- **Board não encontrado:** listar boards disponíveis com `list_boards()` e pedir confirmação
- **Sprint sem dados:** avisar que o board pode não ter sprints configurados
- **Usuário não encontrado:** pedir nome mais específico ou email
- **JQL inválido:** reportar o erro e sugerir reformulação
