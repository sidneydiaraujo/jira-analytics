---
name: jira-analytics
description: "Analise inteligente de dados do Jira e Azure DevOps para os projetos TPROJ, TNP, TLIGHTDIST, TLIGHTCOM, THP, TTRD, TSRV, PROJTHUN, SUP e TVAR. Use esta skill sempre que o usuario quiser entender, analisar ou consultar dados do Jira — sem alterar nada. Acione para perguntas como: 'como esta o sprint?', 'quem esta sobrecarregado?', 'qual a velocidade do time?', 'quais epicos estao em risco?', 'mostre o progresso do TPROJ-123', 'quem entregou mais historias?', 'qual sprint foi o melhor?', 'tem algum epico sem responsavel?', 'qual a taxa de conclusao?', 'quais historias tem risco de atraso?', 'como esta a confianca de entrega do time?', 'quanto tempo cada historia passou em cada status?', 'qual o criterio de aceite de X?', 'o que foi combinado sobre Y?', 'tem algum cenario de teste para Z?', 'busca nas descricoes sobre migração', 'qual PR foi feito para essa historia?', 'que codigo foi implementado no TSRV-123?', 'tem PR aberto para esse item?', 'qual branch foi usada?', 'quais commits foram feitos?', 'analise o ticket X para N1', 'onde foi implementada a funcionalidade Y?', 'em qual PR foi entregue o TSRV-123?', 'quantas horas o Felipe lancou no ultimo mes?', 'qual o total de horas apontadas no TPROJ?', 'quem apontou mais horas esse mes?', 'horas lancadas por pessoa', 'worklog do time', 'quanto o time gastou em teste?', 'horas de QA no sprint', 'tempo em homologacao', 'horas de teste por pessoa', ou qualquer consulta analitica, busca de conteudo, rastreamento de codigo, analise de horas ou analise N1 sobre sprints, responsaveis, epicos, issues, PRs (Jira + Azure DevOps) e funcionalidades. Esta skill e somente leitura — nao altera nenhum dado."
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

## Configuração do Usuário (Personalização)

A skill mantém um perfil persistente do usuário em `~/.claude/jira-analytics-user-config.json`.

### O que é armazenado

| Campo | Descrição | Exemplo |
|---|---|---|
| `default_board` | Board usado quando o usuário não especifica | `86` |
| `boards` | Aliases de boards por nome amigável | `"projetos": 86` |
| `developers` | Desenvolvedores monitorados pelo usuário | `["Anderson", "Felipe", "Vinicios"]` |
| `topics` | Assuntos de interesse para buscas rápidas | `["CCEE", "garantia"]` |

### Regras de uso automático

- **Board não especificado:** use `default_board()` — nunca pergunte ao usuário qual board ele quer se já foi configurado
- **Nome de board mencionado** (ex: "board Projetos", "board da Comercializadora"): resolva via `resolve_board(nome)` antes de chamar qualquer função
- **Desenvolvedores:** quando o usuário disser "meus devs", "o time", "os desenvolvedores" — use a lista `developers` da config
- **Assunto de interesse:** quando o usuário disser "busca sobre CCEE" sem mais contexto — use `topics` como ponto de partida

### Comandos de atualização

Quando o usuário pedir para salvar/adicionar/remover algo, use `update_user_config()`:

| O usuário diz | Ação |
|---|---|
| "salva esse board como meu padrão" | `update_user_config("set_default_board", board_id=86)` |
| "adiciona o board Light Comercializadora (346)" | `update_user_config("add_board", alias="Light Comercializadora", board_id=346)` |
| "remove o board Distribuidora" | `update_user_config("remove_board", alias="distribuidora")` |
| "adiciona o João no meu time" | `update_user_config("add_developer", name="João")` |
| "remove o Vinicios do meu perfil" | `update_user_config("remove_developer", name="Vinicios")` |
| "quero acompanhar o assunto CCEE" | `update_user_config("add_topic", topic="CCEE")` |
| "mostra minha configuração" / "meu perfil" | `get_user_config()` → exibir formatado |

### Como exibir a configuração

```python
from scripts.config_manager import show_config
print(show_config())
```

### Atualização via CLI

```bash
python scripts/config_manager.py --list
python scripts/config_manager.py --add-board "Light Comercializadora" 346
python scripts/config_manager.py --default-board 86
python scripts/config_manager.py --add-dev "João"
python scripts/config_manager.py --add-topic "CCEE"
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

### Módulo 5 — Pesquisa Avançada de Conteúdo

**Gatilhos:** "critério de aceite", "cenário de teste", "o que foi combinado", "o que foi decidido", "busca nas descrições", "tem algo sobre X", "o que está escrito sobre", "acordo nos comentários", "regra de negócio de", qualquer busca por conteúdo dentro de issues

#### `search_content(query, project_keys=None, days=None, max_results=20)`

Pesquisa em linguagem natural dentro do conteúdo completo dos issues:
- **Descrições** — regras de negócio, requisitos, detalhamento da funcionalidade
- **Critérios de aceite** — textos AC:, acceptance criteria, blocos de aceite
- **Cenários de teste** — BDD/Gherkin, Dado que / Quando / Então, cenários descritos
- **Comentários** — decisões, combinados, alinhamentos, histórico de discussões

**Como funciona:**
1. Parser de linguagem natural extrai termos relevantes, remove stopwords PT/EN
2. Detecta filtros de data na query: *"últimos 30 dias"*, *"esta semana"*, *"este mês"*
3. Detecta campo-alvo: palavras como *"comentário"*, *"combinado"* → prioriza `comment`; *"critério"*, *"cenário"* → prioriza `description`
4. Expande sinônimos contextuais para ranking local (não afetam o JQL)
5. Gera JQL inteligente com 1–2 termos âncora (evita zero resultados)
6. Retorna resultado com trecho contextualizado e indicação do campo onde foi encontrado

**Parâmetros:**
- `query` — pergunta em linguagem natural ou palavras-chave (suporta aspas para frase exata: `'"tag contrato"'`)
- `days` — limitar a issues atualizados nos últimos N dias (também detectado na query)
- `project_keys` — lista de projetos específicos; se omitido, busca em todos os 10 projetos
- `max_results` — padrão 20

**Formato do resultado por issue:**
```
key, resumo, tipo, status, responsavel, criado, atualizado, total_comentarios
matches: [{campo, trecho}]  ← campo pode ser "descricao", "comentario — Autor (data)"
```

**Exemplos de queries:**
```
"critério de aceite para o campo contrato CCEE"
"o que foi combinado sobre fatura de venda"
"cenário de teste para migração de ativo"
'"tag contrato" nos últimos 30 dias'
"decisão sobre regra de garantia nos comentários"
"o que foi alinhado sobre integração CCEE"
"BDD para o cadastro da gestora"
```

---

### Módulo 6 — PRs e Código

**Gatilhos:** "PR relacionado", "pull request", "qual código foi implementado", "branch", "commit", "o que foi desenvolvido", "rastrear código", "qual PR fechou essa história", "tem PR associado"

#### `get_issue_prs(issue_key)`
Retorna todos os artefatos de código vinculados a um issue. O ambiente usa **Azure DevOps** para código.

Fontes consultadas (em ordem):
1. **Dev-Status API (Bitbucket)** — PRs, commits e branches via integração Jira-Bitbucket (`/rest/dev-status/latest/issue/detail`)
2. **Campo GMUD** (`customfield_11536`) — link para a página wiki do Azure DevOps com a documentação de deploy/mudança da feature
3. **Remote Issue Links** — links colados manualmente via "Vincular" no Jira
4. **Fallback textual** — busca regex por URLs de PR/commit do Azure DevOps, Bitbucket, GitHub ou GitLab na descrição e comentários do issue

Retorna:
- `pull_requests` — PRs do Bitbucket (quando integração ativa)
- `commits` / `branches` — do Bitbucket
- `remote_links` — inclui o link GMUD (type: "gmud") com URL da wiki Azure DevOps
- `text_links` — URLs de PR/commit encontradas no texto

> **Nota:** A maioria dos épicos tem o campo GMUD preenchido. Para histórias/subtarefas, o link pode estar nos comentários ou remote links.

#### `get_prs_for_stories(issue_keys)`
Busca PRs em batch para uma lista de issues. Útil para verificar quais histórias de um sprint têm PR associado.
- Retorna `[{key, total_prs, total_commits, prs, branches, remote_links}]`

#### `find_story_by_pr_pattern(issue_key, pr_url_pattern=None)`
Visão consolidada de rastreabilidade de código para um issue:
- Informações do issue (resumo, status, responsável)
- Resumo: total de PRs, PRs merged, PRs abertos, commits, branches
- Detalhe completo de cada artefato
- Aceita filtro opcional por padrão de URL (ex: nome do repositório)

**Exemplos de uso:**

| O usuário pergunta | Ação |
|---|---|
| "Qual PR implementou o TSRV-1263?" | `get_issue_prs("TSRV-1263")` |
| "Quais histórias do sprint têm PR?" | `get_prs_for_stories([lista de keys])` |
| "Mostre o código implementado no TPROJ-9651" | `find_story_by_pr_pattern("TPROJ-9651")` |
| "Tem PR aberto para o TLIGHTCOM-555?" | `get_issue_prs` — filtra `status != MERGED` |
| "Qual branch foi usada no TSRV-1264?" | `get_issue_prs` — campo `branches` |
| "Quais commits foram feitos no TPROJ-7460?" | `find_story_by_pr_pattern` — campo `commits` |

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

### Módulo 9 — Worklog Hours: Análise de Horas Apontadas

**Gatilhos:** "quantas horas o X lançou", "horas apontadas no último mês", "total de horas do time", "quem apontou mais horas", "worklog do Felipe", "horas por projeto", "quanto tempo foi lançado em Y", "apontamentos de horas", "quanto tempo o time gastou em teste", "horas de QA", "tempo em homologação"

---

#### Regra de comportamento — quando mostrar horas de teste

- **Só mostrar classificação de teste** (coluna "Teste", % teste, issues de teste) quando o usuário **pedir explicitamente**: "horas de teste", "tempo em QA", "horas em homologação", "atividades de teste", "quanto foi gasto em teste"
- **Quando o usuário pedir apenas o total de horas** (sem mencionar teste): mostrar somente total, por pessoa e por projeto — sem a coluna de teste
- **Quando a análise revelar padrão de teste relevante** (ex: >20% do tempo de alguém): sugerir ao usuário que quer ver o detalhamento — **não mostrar automaticamente**

> Exemplo de sugestão: "Tatiana Lima concentrou 17.5% do tempo em atividades de teste. Quer ver o detalhe por issue de teste?"

---

#### Como funciona

Usa a API de worklog em bulk do Jira Cloud:
1. `GET /worklog/updated?since={ms}` — busca IDs de todos os worklogs do período (paginado)
2. `POST /worklog/list` — busca detalhes de até 1000 worklogs por requisição
3. Resolve `issueId → issueKey/projeto/status` via JQL em batch
4. Agrega por pessoa, projeto e issue
5. **Classificação de teste** (quando solicitada): verifica status e summary da issue — identifica automaticamente issues de teste pelo nome ("Testes Integrados", "Testes Unitários", "Evidências", "Homologação") e pelo status no Jira

> **Nota sobre comentários:** os worklogs do Jira geralmente não têm comentário preenchido neste time. A classificação de teste usa o nome da issue e o status — não o comentário.

> **Nota sobre nomes compostos:** o filtro de pessoa usa palavras individuais. "Felipe Sartor" encontra "Felipe de Bona Sartor" corretamente.

---

#### `get_worklog_hours(since_days, person, project_keys, keyword)` — horas brutas

| Parâmetro | Tipo | Descrição |
|---|---|---|
| `since_days` | int | Período em dias (padrão 30) — ex: 7, 21, 90 |
| `person` | str | Nome parcial do autor (ex: `"Felipe"`, `"Anderson"`) |
| `project_keys` | list | Filtrar por projeto (ex: `["TPROJ", "TSRV"]`) |
| `keyword` | str | Palavra no comentário do worklog (ex: `"teste"`) |

Retorna: `total_horas`, `total_lancamentos`, `por_pessoa[]`, `por_projeto[]`, `por_issue[]`, `detalhes[]`

---

#### `analyze_worklog_hours(since_days, person, project_keys, group_name, classify_test)` — horas com classificação de teste

| Parâmetro | Tipo | Descrição |
|---|---|---|
| `since_days` | int | Período em dias (padrão 21) |
| `person` | str | Nome parcial do autor |
| `project_keys` | list | Filtrar por projeto (ex: `["TPROJ"]`) |
| `group_name` | str | Nome do grupo Jira (ex: `"[DEV] Projetos"`) — fallback para projeto se não encontrado |
| `classify_test` | bool | Se True, classifica cada worklog como teste ou não (padrão True) |

Retorna: `total_horas`, `total_teste`, `pct_teste`, `por_pessoa[]` (com campos `teste` e `pct_teste`), `por_issue_teste[]`, `detalhes_teste[]`, `aviso`

**Critérios de classificação de teste:**
- Status da issue contém: "em teste", "homologação", "qa", "in review", "validação"
- Summary da issue contém: "teste", "test", "qa", "homolog", "evidenci", "regressão"

**Fallback de grupo:** se `group_name` não for encontrado via API de grupos, filtra automaticamente pelo `project_keys` informado (ou TPROJ por padrão) e inclui aviso no retorno.

---

#### Exemplos de uso

| O usuário pergunta | Ação |
|---|---|
| "Quantas horas o Felipe lançou no último mês?" | `get_worklog_hours(30, person="Felipe")` → mostrar total por pessoa e projeto |
| "Qual o total de horas do time no TPROJ esta semana?" | `get_worklog_hours(7, project_keys=["TPROJ"])` → mostrar totais |
| "Quem apontou mais horas esse mês?" | `get_worklog_hours(30)` → ordenar por total decrescente |
| "Quanto o time gastou em teste nas últimas 3 semanas?" | `analyze_worklog_hours(21, project_keys=["TPROJ"], classify_test=True)` → mostrar coluna teste + % |
| "Horas de teste do time [DEV] Projetos" | `analyze_worklog_hours(21, group_name="[DEV] Projetos", classify_test=True)` |
| "Quanto o Felipe gastou em QA no último mês?" | `analyze_worklog_hours(30, person="Felipe Sartor", classify_test=True)` |

---

#### Como apresentar a resposta

**Quando pedido apenas total de horas (sem teste):**
- Tabela: Pessoa | Total | Lançamentos
- Tabela: Projeto | Horas | % do total
- Top 5 issues com mais horas
- Período consultado

**Quando pedido horas de teste:**
- Tabela: Pessoa | Total | Teste | % Teste | Lançamentos de teste
- Issues classificadas como teste (ticket, horas, resumo)
- Lançamentos de teste dia a dia (quando relevante)
- Sugerir drill-down se alguma pessoa tiver % muito alta ou muito baixa
- Se nenhuma hora de teste encontrada: explicar os critérios usados para classificação

**Sugestão proativa (sem pedir):**
- Ao mostrar horas totais, se uma pessoa tiver >20% do tempo em issues de teste, acrescentar no final: "Tatiana Lima tem 17.5% do tempo em atividades de teste. Quer ver o detalhe?"
- Não mostrar a tabela de teste completa sem que o usuário peça

---

### Módulo 8 — Análise Estratégica: Miro + Jira

**Gatilhos:** "analise o board Miro", "cruze com o backlog", "o que precisa ser priorizado", "planejamento estratégico vs tickets", "analise a esteira B2B2C", "leia o Miro e cruze com o Jira"

#### Como ler o Miro (dois modos)

**Modo 1 — Conector nativo (preferido em sessões Claude)**
Use `mcp__claude_ai_Miro__context_get(miro_url=...)` — retorna resumo estruturado gerado por IA com áreas, métricas, objetivos e relações. Disponível quando o plugin Miro está ativo na sessão.

**Modo 2 — REST API (fallback / scripts Python autônomos)**
Usa `MIRO_API_TOKEN` + `read_miro_board(url_or_id)` — lê todos os itens via paginação (frames, cards, sticky notes, textos). Mais granular, menos interpretado.

**Fluxo recomendado:**
1. Tente `mcp__claude_ai_Miro__context_get` primeiro (disponível na sessão)
2. Se não disponível, use `read_miro_board` com o token
3. Passe o conteúdo extraído para `analyze_strategic_backlog(miro_context=...)` para cruzar com o Jira

#### `read_miro_board(url_or_id)`
Lê o board via REST API. Retorna: `frames`, `cards`, `sticky_notes`, `texts`, `raw_text`.
Requer: `MIRO_API_TOKEN` em settings.json.

#### `analyze_strategic_backlog(miro_url, jira_project, since_date, miro_context, ...)`
Cruzamento Miro + Jira para relatório de priorização.

Parâmetros principais:
- `miro_url_or_id` — URL/ID do board (usa REST API)
- `jira_project` — projeto Jira (ex: `"MP"`)
- `since_date` — data de corte (ex: `"2024-01-01"`)
- `miro_context` — texto do Miro colado/extraído manualmente (fallback sem token)
- `max_tickets` — máximo de tickets (padrão 100)

Retorna relatório com: `descricao`, `categoria`, `clientes`, `prioridade`, `status`, `assignee`, `solucao_proposta`.

#### Token Miro
Configurado em `settings.json` como `MIRO_API_TOKEN`.
Para obter: [miro.com/app/settings/user-profile/apps](https://miro.com/app/settings/user-profile/apps) → Create app → scope `boards:read` → Install e copiar token.

---

### Módulo 7 — Azure DevOps: PRs, Commits e Análise N1

**Gatilhos:** "em qual PR foi implementado", "qual branch", "qual commit entregou", "onde está o código do TSRV-X", "analise o ticket X", "faça uma análise N1 do TSRV-X", "pesquisa no Azure", "qual PR no Azure"

#### Configuração de credenciais Azure DevOps

Variáveis de ambiente necessárias (além das do Jira):

| Variável | Descrição | Exemplo |
|---|---|---|
| `AZURE_DEVOPS_ORG` | Nome da organização no Azure DevOps | `thunderstech` |
| `AZURE_DEVOPS_PAT` | Personal Access Token com escopo `Code: Read` | `abc123...` |
| `AZURE_DEVOPS_PROJECT` | Projeto padrão para pesquisa | `ThundersProject` |

> Se as variáveis não estiverem configuradas, as funções Azure retornam vazio e a skill informa que o Azure não está disponível. O restante da skill continua funcionando normalmente com dados do Jira.

#### `search_azure_prs(jira_key, az_project=None)`

Busca PRs no Azure DevOps relacionados a uma chave Jira.

**Estratégia em cascata:**
1. **Azure DevOps PR Search API** — busca rápida cross-repo por texto livre (requer extensão Code Search habilitada)
2. **Fallback: varredura de repositórios** — lista todos os repos do projeto e filtra PRs onde título, branch ou descrição contém a chave (client-side, últimos 180 dias)

Retorna: `[{pr_id, title, status, url, repo, author, created, completed, source_branch, target_branch, merged, description, via}]`

#### `search_azure_commits(jira_key, az_project=None, days=90)`

Busca commits cujo commit message menciona a chave Jira.

Usa `searchCriteria.comment` do Azure DevOps (filtro server-side — eficiente). Pesquisa em todos os repositórios do projeto nos últimos `days` dias.

Retorna: `[{commit_id, message, author, date, repo, url}]`

#### `get_azure_pr_files(org, project, repo, pr_id)`

Retorna lista de arquivos alterados em um PR específico do Azure DevOps.

Busca a última iteração do PR (estado final das mudanças). Útil para entender o escopo de implementação.

Retorna: `[{file, change_type}]`

#### `analyze_ticket_n1(issue_key, include_pr_files=False)`

**Função principal do N1.** Combina todas as fontes de dados em um relatório consolidado.

**O que analisa:**
1. **Jira**: tipo, status, assignee, descrição completa, critérios de aceite, últimos 5 comentários, fix versions, link GMUD
2. **Hierarquia**: issue pai (história → épico → contexto de negócio)
3. **Azure DevOps PRs**: busca por chave Jira no título/branch/descrição, com fallback para o épico pai
4. **Azure DevOps Commits**: commits com a chave no message
5. **Jira Code**: PRs via Dev-Status API (Bitbucket) + links remotos + URLs nos textos
6. **GMUD**: link para documentação de deploy/mudança no Azure DevOps Wiki

**Retorna dict com:**
```
issue          — resumo, status, assignee, tipo, datas, fix_versions
hierarquia     — parent_key, parent_summary, parent_status
descricao      — texto completo (até 2000 chars)
ultimos_comentarios — últimos 5 comentários com autor e data
azure          — prs[], commits[], pr_merged_count, disponivel
jira_code      — pull_requests[], remote_links[], text_links[], commits[]
gmud           — texto, urls[]
implementacao  — tem_pr_merged, repos_com_pr[], branches[], merged_em
```

**Parâmetro `include_pr_files=True`:** adiciona `files_changed` a cada PR Azure (chamada extra por PR — use apenas quando realmente necessário).

---

## Como a Skill Responde ao N1

Quando o usuário pedir análise N1 de um ticket:

1. Chame `analyze_ticket_n1(issue_key)`
2. Apresente o resultado em formato estruturado:
   - **Ticket**: tipo, status, responsável, resumo
   - **Contexto**: épico pai + descrição resumida
   - **Implementação**: PRs encontrados (Azure + Jira), repos, branches, data de merge
   - **Deploy**: link GMUD se disponível
   - **Últimos comentários**: decisões e combinados recentes
3. Se `azure.disponivel = False`, informe que o Azure não está configurado e mostre apenas os dados do Jira (PRs via Dev-Status + links remotos)
4. Se nenhum PR for encontrado, avise e sugira pesquisar pelo épico pai ou por commits

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
- `search_content(query, project_keys, days, max_results)` — pesquisa em linguagem natural em descrições, critérios, cenários e comentários
- `get_issue_prs(issue_key)` — PRs, commits e branches vinculados a um issue via Jira + remote links
- `get_prs_for_stories(issue_keys)` — PRs em batch para uma lista de issues
- `find_story_by_pr_pattern(issue_key, pr_url_pattern)` — visão consolidada de rastreabilidade de código
- `search_azure_prs(jira_key, az_project)` — PRs no Azure DevOps por chave Jira (Search API + fallback)
- `search_azure_commits(jira_key, az_project, days)` — commits no Azure com chave no message
- `get_azure_pr_files(org, project, repo, pr_id)` — arquivos alterados em um PR do Azure
- `analyze_ticket_n1(issue_key, include_pr_files)` — análise N1 completa: Jira + Azure + GMUD
- `read_miro_board(url_or_id)` — lê conteudo completo de um board Miro (requer MIRO_API_TOKEN)
- `analyze_strategic_backlog(miro_url, jira_project, since_date, ...)` — cruzamento Miro + Jira para priorizacao estrategica

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
| "Qual o critério de aceite para o campo CCEE?" | `search_content` — snippet da descrição com o trecho exato |
| "O que foi combinado sobre a fatura de venda?" | `search_content` — snippets de comentários com autor e data |
| "Tem cenário de teste para migração de ativo?" | `search_content` — busca com hint de campo `description` |
| "O que foi decidido sobre garantia nos comentários?" | `search_content` com `field_hint=comment` |
| "Busca 'tag contrato' nos últimos 30 dias" | `search_content` com frase exata e filtro de data |
| "Analise o ticket TSRV-1263 para N1" | `analyze_ticket_n1("TSRV-1263")` |
| "Em qual PR foi implementado o TLIGHTCOM-987?" | `search_azure_prs("TLIGHTCOM-987")` |
| "Quais commits mencionam o TPROJ-10395?" | `search_azure_commits("TPROJ-10395")` |
| "Quais arquivos foram alterados no PR #42?" | `get_azure_pr_files(org, project, repo, 42)` |
| "Onde está o código da funcionalidade RF029?" | `analyze_ticket_n1` + `search_azure_prs` pelo épico |
| "Analise o board Miro e cruze com o backlog MP" | `analyze_strategic_backlog(miro_url, "MP", "2024-01-01")` |
| "O que precisa ser priorizado na esteira B2B2C?" | `analyze_strategic_backlog` — relatorio priorizacao |
| "Leia o board Miro e identifique os temas estratégicos" | `read_miro_board(url)` — frames + cards + sticky notes |

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
