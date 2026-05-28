---
name: jira-analytics
description: "Analise inteligente de dados do Jira para os projetos TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP e TVAR. Use esta skill sempre que o usuario quiser entender, analisar ou consultar dados do Jira — sem alterar nada. Acione para perguntas como: 'como esta o sprint?', 'quem esta sobrecarregado?', 'qual a velocidade do time?', 'quais epicos estao em risco?', 'mostre o progresso do TPROJ-123', 'quem entregou mais historias?', 'qual sprint foi o melhor?', 'tem algum epico sem responsavel?', 'qual a taxa de conclusao?', 'quais historias tem risco de atraso?', 'como esta a confianca de entrega do time?', 'quanto tempo cada historia passou em cada status?', ou qualquer consulta analitica sobre sprints, responsaveis, epicos ou issues no Jira. Esta skill e somente leitura — nao altera nenhum dado."
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

## Regras de Comportamento

- **Sprints:** sempre usar o sprint ativo por padrão. Se não houver sprint ativo, usar o último fechado. O usuário precisa especificar explicitamente se quiser dados de sprints anteriores.
- **Histórias vs Subtarefas:** todas as análises de sprint operam apenas em histórias. Subtarefas são excluídas por padrão (a não ser que o usuário peça explicitamente dados de subtarefas).
- **Somente leitura:** nenhuma escrita, nenhuma transição, nenhuma modificação de dados.

---

## Módulos de Análise

### Módulo 1 — Sprints

**Gatilhos:** "como está o sprint", "velocidade do time", "taxa de conclusão", "sprint atual", "historias não concluídas", "historias em andamento", "risco de atraso"

#### `get_sprint_report(board_id, sprint_id=None)`
Métricas consolidadas do sprint (somente histórias):
- Total de histórias, concluídas, em andamento, não iniciadas
- Taxa de conclusão (%)
- Story points totais e concluídos
- Histórias sem responsável
- Lista das não concluídas

Se `sprint_id` não for informado, usa o sprint ativo. Se não houver ativo, usa o último fechado.

#### `get_sprint_stories_detail(board_id, sprint_id=None, status_filter=None)`
Histórias com análise detalhada de tempo e risco (somente histórias, sem subtarefas):
- Tempo atual no status (`days_in_status`)
- Estimativa original, tempo gasto, tempo restante
- **Risco de atraso:** CRITICO / ALTO / MEDIO / BAIXO / SEM_ESTIMATIVA / N/A
- Histórico completo de status (`status_history`)
- Ordenado por risco (mais crítico primeiro)

`status_filter`: `'in_progress'`, `'todo'`, `'done'` ou `None` (todas)

> Use quando o usuário pedir "histórias em andamento", "quais histórias têm risco?", "quais histórias estão atrasadas?", "mostre detalhes das histórias"

#### `get_sprint_velocity(board_id, num_sprints=5)`
Velocidade dos últimos N sprints fechados:
- Story points e histórias entregues por sprint
- Taxa de conclusão por sprint
- Média de velocidade

#### `get_issue_time_in_status(issue_key)`
Tempo detalhado que um issue específico passou em cada status:
- Histórico completo: status → data início → data fim → horas
- Estimativa original, tempo gasto, tempo restante
- Tempo atual no status corrente

---

### Módulo 2 — Responsáveis e Métricas por Desenvolvedor

**Gatilhos:** "quem está sobrecarregado", "carga do time", "produtividade", "quem entregou mais", "distribuição de trabalho", "confiança de entrega", "erro de estimativa", "métricas do time"

#### `get_assignee_workload(project_keys=None)`
Carga atual de todos os responsáveis nos sprints abertos (somente histórias):
- Total de issues, concluídas, em andamento, não iniciadas
- Story points por pessoa
- Ordenado do mais carregado para o menos

#### `get_assignee_productivity(assignee_query, days=30)`
Produtividade individual nos últimos N dias:
- Issues e story points entregues
- Issues e story points em aberto
- Busca o usuário por nome ou email

#### `get_developer_metrics(board_id, num_sprints=5)`
Métricas ágeis por desenvolvedor com base nos últimos N sprints fechados:

| Métrica | Descrição |
|---|---|
| **Taxa de entrega** | % de histórias concluídas vs comprometidas por sprint |
| **Variação de entrega** | Desvio padrão da taxa (consistência) |
| **Confiança de entrega** | ALTA / MEDIA / BAIXA / CRITICA / SEM_DADOS |
| **Throughput médio** | Histórias entregues por sprint |
| **Erro de estimativa (MAPE)** | Desvio médio entre estimativa original e tempo real |
| **Precisão de estimativa** | PRECISO / ACEITAVEL / IMPRECISO / MUITO_IMPRECISO |

**Critérios de confiança:**
- ALTA: média ≥ 85% e desvio ≤ 10%
- MEDIA: média ≥ 70% e desvio ≤ 20%
- BAIXA: média ≥ 50%
- CRITICA: média < 50%

**Critérios de precisão (MAPE):**
- PRECISO: erro ≤ 20%
- ACEITAVEL: erro ≤ 40%
- IMPRECISO: erro ≤ 70%
- MUITO_IMPRECISO: erro > 70%

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
Progresso detalhado de um épico específico (separa histórias de subtarefas):
- Total de histórias e subtarefas
- % de conclusão
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

- **Tabelas** para comparações (ex: carga por responsável, métricas por desenvolvedor)
- **Listas** para itens de atenção (ex: épicos em risco, histórias com risco CRITICO)
- **Métricas em destaque** para resumos (ex: taxa de conclusão 78%, confiança ALTA)
- **Recomendações** quando identificar padrões de risco (ex: dev com MAPE > 70%, sprint com muitas histórias sem estimativa)

---

## Scripts Disponíveis

### `scripts/analyzer.py`
Módulos de análise reutilizáveis:
- `list_boards(project_key)` — lista boards disponíveis
- `get_sprint_report(board_id, sprint_id)` — relatório consolidado de sprint (somente histórias)
- `get_sprint_stories_detail(board_id, sprint_id, status_filter)` — histórias com time-in-status e risco de atraso
- `get_sprint_velocity(board_id, num_sprints)` — velocidade histórica
- `get_issue_time_in_status(issue_key)` — tempo detalhado por status via changelog
- `get_developer_metrics(board_id, num_sprints)` — métricas ágeis por desenvolvedor
- `get_assignee_workload(project_keys)` — carga por responsável
- `get_assignee_productivity(assignee_query, days)` — produtividade individual
- `get_epics_health(project_keys)` — diagnóstico de épicos
- `get_epic_progress(epic_key)` — progresso de um épico (histórias vs subtarefas)
- `free_query(jql, fields, max_results)` — consulta JQL livre

---

## Exemplos de Uso

| O usuário pergunta | Ação |
|---|---|
| "Como está o sprint atual do TPROJ?" | `get_sprint_report` no board do TPROJ |
| "Mostre as histórias em andamento do sprint" | `get_sprint_stories_detail` com `status_filter='in_progress'` |
| "Quais histórias têm risco de atraso?" | `get_sprint_stories_detail` — ordena por risco, filtra CRITICO/ALTO |
| "Quanto tempo a TPROJ-123 está em andamento?" | `get_issue_time_in_status("TPROJ-123")` |
| "Qual a velocidade do time nos últimos 5 sprints?" | `get_sprint_velocity` |
| "Quem está mais sobrecarregado?" | `get_assignee_workload` — ordena pelo total |
| "Como foi a produtividade do João este mês?" | `get_assignee_productivity("João", 30)` |
| "Qual a confiança de entrega do time?" | `get_developer_metrics` — exibe confiança por dev |
| "Qual o erro de estimativa médio?" | `get_developer_metrics` — exibe MAPE por dev |
| "Quais épicos estão em risco?" | `get_epics_health` — filtra `em_risco` |
| "Qual o progresso do TPROJ-123?" | `get_epic_progress("TPROJ-123")` |
| "Tem bugs abertos sem responsável?" | `free_query` com JQL adequado |
| "Quais histórias estão atrasadas no sprint?" | `free_query` com `duedate < now()` |

---

## Risco de Atraso — Metodologia

O cálculo combina três fatores:

1. **Tempo gasto vs estimativa original:** se `spent > original_estimate * 1.5` → CRITICO
2. **Projeção de estouro:** `(spent + remaining) / original_estimate` — razão > 1.5 → CRITICO, > 1.2 → ALTO
3. **Tempo no status atual sem log de tempo:** se está ativo há 2+ dias sem registrar horas → MEDIO (falta de transparência)

Sem estimativa original: issue em andamento há 3+ dias → ALTO; caso contrário → SEM_ESTIMATIVA.

---

## Tratamento de Erros

- **Board não encontrado:** listar boards disponíveis com `list_boards()` e pedir confirmação
- **Sprint sem dados:** avisar que o board pode não ter sprints configurados
- **Usuário não encontrado:** pedir nome mais específico ou email
- **JQL inválido:** reportar o erro e sugerir reformulação
