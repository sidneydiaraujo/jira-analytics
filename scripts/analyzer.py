"""
Jira Analytics — modulo de analise de sprints, responsaveis, epicos e consultas livres.
Reutiliza autenticacao e chamadas da jira-epic-automator via JIRA_EMAIL + JIRA_API_TOKEN.
"""
import os
import sys
import requests
from datetime import date, datetime, timedelta
from collections import defaultdict

JIRA_BASE = "https://qx3prod.atlassian.net/rest/api/3"
JIRA_AGILE = "https://qx3prod.atlassian.net/rest/agile/1.0"
PROJECTS = ["TPROJ", "TNP", "TLIGHTDIST", "TLIGHTCOM", "THP", "TTRD", "TSRV", "PROJTHUN", "SUP", "TVAR"]


def _auth():
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not email or not token:
        raise EnvironmentError("JIRA_EMAIL e JIRA_API_TOKEN sao obrigatorios.")
    return (email, token)


def _get(path, params=None, base=JIRA_BASE):
    r = requests.get(f"{base}{path}", auth=_auth(),
                     headers={"Accept": "application/json"}, params=params)
    r.raise_for_status()
    return r.json()


def _post(path, body, base=JIRA_BASE):
    r = requests.post(f"{base}{path}", auth=_auth(),
                      headers={"Accept": "application/json", "Content-Type": "application/json"},
                      json=body)
    r.raise_for_status()
    return r.json() if r.content else {}


def _search(jql, fields, max_results=500):
    results = []
    next_token = None
    fields_list = fields if isinstance(fields, list) else fields.split(",")
    while True:
        payload = {"jql": jql, "fields": fields_list, "maxResults": min(100, max_results)}
        if next_token:
            payload["nextPageToken"] = next_token
        data = _post("/search/jql", payload)
        batch = data.get("issues", [])
        results.extend(batch)
        next_token = data.get("nextPageToken")
        if not next_token or len(results) >= max_results or len(batch) < 100:
            break
    return results


# ---------------------------------------------------------------------------
# Modulo 1: Sprints
# ---------------------------------------------------------------------------

def list_boards(project_key=None):
    """Lista todos os boards acessiveis, opcionalmente filtrando por projeto."""
    params = {"maxResults": 50}
    if project_key:
        params["projectKeyOrId"] = project_key
    data = _get("/board", params=params, base=JIRA_AGILE)
    return data.get("values", [])


def get_sprint_report(board_id: int, sprint_id: int = None):
    """Retorna metricas de um sprint especifico ou do sprint ativo de um board.

    Metricas: total de historias, concluidas, em andamento, nao iniciadas,
    taxa de conclusao, story points (se preenchidos), lista de nao concluidas.
    """
    if sprint_id:
        sprint = _get(f"/sprint/{sprint_id}", base=JIRA_AGILE)
    else:
        sprints = _get(f"/board/{board_id}/sprint",
                       params={"state": "active"}, base=JIRA_AGILE)
        values = sprints.get("values", [])
        if not values:
            sprints = _get(f"/board/{board_id}/sprint",
                           params={"state": "closed"}, base=JIRA_AGILE)
            values = sorted(sprints.get("values", []),
                            key=lambda s: s.get("endDate", ""), reverse=True)
        if not values:
            return {"erro": f"Nenhum sprint encontrado no board {board_id}"}
        sprint = values[0]

    sprint_id = sprint["id"]
    sprint_name = sprint.get("name", f"Sprint {sprint_id}")
    sprint_state = sprint.get("state", "")
    start = sprint.get("startDate", "")[:10] if sprint.get("startDate") else "—"
    end = sprint.get("endDate", "")[:10] if sprint.get("endDate") else "—"

    issues = _get(f"/board/{board_id}/sprint/{sprint_id}/issue",
                  params={"maxResults": 500,
                          "fields": "summary,status,assignee,story_points,customfield_10016,resolutiondate"},
                  base=JIRA_AGILE).get("issues", [])

    done, in_progress, todo, no_assignee = [], [], [], []
    total_points, done_points = 0, 0

    for issue in issues:
        f = issue["fields"]
        cat = f.get("status", {}).get("statusCategory", {}).get("key", "")
        points = f.get("customfield_10016") or 0
        total_points += points
        assignee = (f.get("assignee") or {}).get("displayName", "Sem responsavel")
        if assignee == "Sem responsavel":
            no_assignee.append(issue["key"])

        if cat == "done":
            done.append(issue["key"])
            done_points += points
        elif cat == "indeterminate":
            in_progress.append(issue["key"])
        else:
            todo.append(issue["key"])

    total = len(issues)
    taxa = round(len(done) / total * 100, 1) if total else 0

    return {
        "sprint": sprint_name,
        "estado": sprint_state,
        "periodo": f"{start} a {end}",
        "total_historias": total,
        "concluidas": len(done),
        "em_andamento": len(in_progress),
        "nao_iniciadas": len(todo),
        "taxa_conclusao": f"{taxa}%",
        "story_points_total": total_points,
        "story_points_concluidos": done_points,
        "sem_responsavel": no_assignee,
        "nao_concluidas": in_progress + todo,
    }


def get_sprint_velocity(board_id: int, num_sprints: int = 5):
    """Calcula velocidade media dos ultimos N sprints fechados."""
    sprints_data = _get(f"/board/{board_id}/sprint",
                        params={"state": "closed", "maxResults": num_sprints},
                        base=JIRA_AGILE)
    sprints = sorted(sprints_data.get("values", []),
                     key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]

    velocities = []
    for sprint in sprints:
        issues = _get(f"/board/{board_id}/sprint/{sprint['id']}/issue",
                      params={"maxResults": 500,
                              "fields": "status,customfield_10016"},
                      base=JIRA_AGILE).get("issues", [])
        pts = sum(
            (i["fields"].get("customfield_10016") or 0)
            for i in issues
            if i["fields"].get("status", {}).get("statusCategory", {}).get("key") == "done"
        )
        velocities.append({
            "sprint": sprint.get("name"),
            "points_concluidos": pts,
        })

    avg = round(sum(v["points_concluidos"] for v in velocities) / len(velocities), 1) if velocities else 0
    return {
        "board_id": board_id,
        "sprints_analisados": len(velocities),
        "velocidade_media": avg,
        "historico": velocities,
    }


# ---------------------------------------------------------------------------
# Modulo 2: Responsaveis
# ---------------------------------------------------------------------------

def get_assignee_workload(project_keys=None, sprint_state="active"):
    """Analisa carga de trabalho por responsavel nos projetos monitorados."""
    projects = project_keys or PROJECTS
    jql = (
        f'project in ({",".join(projects)}) '
        f'AND issuetype in (Story, Task, Bug) '
        f'AND sprint in openSprints() '
        f'AND assignee is not EMPTY'
    )
    issues = _search(jql, "summary,status,assignee,customfield_10016,priority")

    workload = defaultdict(lambda: {"total": 0, "done": 0, "in_progress": 0,
                                    "todo": 0, "points": 0, "issues": []})
    for issue in issues:
        f = issue["fields"]
        assignee = (f.get("assignee") or {}).get("displayName", "Desconhecido")
        cat = f.get("status", {}).get("statusCategory", {}).get("key", "")
        pts = f.get("customfield_10016") or 0

        workload[assignee]["total"] += 1
        workload[assignee]["points"] += pts
        workload[assignee]["issues"].append(issue["key"])
        if cat == "done":
            workload[assignee]["done"] += 1
        elif cat == "indeterminate":
            workload[assignee]["in_progress"] += 1
        else:
            workload[assignee]["todo"] += 1

    return dict(sorted(workload.items(), key=lambda x: x[1]["total"], reverse=True))


def get_assignee_productivity(assignee_query: str, days: int = 30):
    """Analisa produtividade de um responsavel nos ultimos N dias."""
    since = (date.today() - timedelta(days=days)).isoformat()
    users = requests.get(
        f"{JIRA_BASE}/user/search",
        auth=_auth(),
        headers={"Accept": "application/json"},
        params={"query": assignee_query, "maxResults": 5}
    ).json()

    if not users:
        return {"erro": f"Nenhum usuario encontrado para '{assignee_query}'"}
    user = users[0]
    account_id = user["accountId"]
    display_name = user["displayName"]

    jql = (
        f'assignee = "{account_id}" '
        f'AND resolutiondate >= "{since}" '
        f'AND statusCategory = Done '
        f'AND issuetype in (Story, Task, Bug)'
    )
    done_issues = _search(jql, "summary,resolutiondate,customfield_10016,priority,issuetype")

    jql_open = (
        f'assignee = "{account_id}" '
        f'AND statusCategory != Done '
        f'AND issuetype in (Story, Task, Bug)'
    )
    open_issues = _search(jql_open, "summary,status,customfield_10016,priority,issuetype")

    done_pts = sum(i["fields"].get("customfield_10016") or 0 for i in done_issues)
    open_pts = sum(i["fields"].get("customfield_10016") or 0 for i in open_issues)

    return {
        "responsavel": display_name,
        "periodo_dias": days,
        "concluidas": len(done_issues),
        "story_points_entregues": done_pts,
        "em_aberto": len(open_issues),
        "story_points_em_aberto": open_pts,
        "issues_concluidas": [i["key"] for i in done_issues],
        "issues_em_aberto": [i["key"] for i in open_issues],
    }


# ---------------------------------------------------------------------------
# Modulo 3: Epicos
# ---------------------------------------------------------------------------

def get_epics_health(project_keys=None):
    """Analisa saude dos epicos: progresso, campos faltando, riscos e SLA de garantia."""
    from pathlib import Path
    import json

    projects = project_keys or PROJECTS
    fields_cache = Path.home() / ".claude" / "jira-epic-automator-fields.json"
    custom = {}
    if fields_cache.exists():
        with open(fields_cache) as fp:
            custom = json.load(fp)

    pub_fid = custom.get("field_publication_date", "customfield_11336")
    warranty_fid = custom.get("field_warranty_date", "customfield_12167")
    quarter_fid = custom.get("field_quarter", "customfield_11450")
    start_fid = custom.get("field_start_date", "customfield_11201")
    due_fid = custom.get("field_due_date", "duedate")

    excluded = '"Em Producao", "Em Produção", "Concluido", "Concluído", "Done", "Fechado", "Closed"'
    jql = (
        f'issuetype = Epic AND project in ({",".join(projects)}) '
        f'AND status not in ({excluded})'
    )
    fields = f"summary,status,assignee,{pub_fid},{warranty_fid},{quarter_fid},{start_fid},{due_fid},fixVersions"
    epics = _search(jql, fields, max_results=300)

    today = date.today()
    report = {
        "total": len(epics),
        "sem_responsavel": [],
        "sem_quarter": [],
        "sem_start_date": [],
        "sem_data_publicacao": [],
        "garantia_vencida": [],
        "em_risco": [],
        "saudaveis": [],
    }

    for epic in epics:
        f = epic["fields"]
        key = epic["key"]
        summary = f.get("summary", "")
        issues = []

        if not f.get("assignee"):
            issues.append("sem responsavel")
            report["sem_responsavel"].append(key)
        if not f.get(quarter_fid):
            issues.append("sem quarter")
            report["sem_quarter"].append(key)
        if not f.get(start_fid):
            issues.append("sem start date")
            report["sem_start_date"].append(key)
        if not f.get(pub_fid):
            issues.append("sem data de publicacao")
            report["sem_data_publicacao"].append(key)

        warranty_str = f.get(warranty_fid)
        if warranty_str:
            warranty_date = date.fromisoformat(warranty_str[:10])
            if today > warranty_date:
                issues.append(f"garantia vencida em {warranty_str[:10]}")
                report["garantia_vencida"].append(key)

        if issues:
            report["em_risco"].append({"key": key, "summary": summary, "issues": issues})
        else:
            report["saudaveis"].append(key)

    return report


def get_epic_progress(epic_key: str):
    """Retorna progresso detalhado de um epico: historias por status, % conclusao."""
    issues = _search(
        f'"Epic Link" = {epic_key} OR parent = {epic_key}',
        "summary,status,assignee,customfield_10016,issuetype",
        max_results=200
    )

    if not issues:
        return {"epic": epic_key, "erro": "Nenhuma historia encontrada"}

    by_status = defaultdict(list)
    total_pts, done_pts = 0, 0
    for issue in issues:
        f = issue["fields"]
        status = f.get("status", {}).get("name", "Desconhecido")
        cat = f.get("status", {}).get("statusCategory", {}).get("key", "")
        pts = f.get("customfield_10016") or 0
        total_pts += pts
        if cat == "done":
            done_pts += pts
        by_status[status].append(issue["key"])

    total = len(issues)
    done_count = sum(len(v) for k, v in by_status.items()
                     if _search(f'issue = {v[0]}', "status")[0]["fields"]
                     ["status"]["statusCategory"]["key"] == "done") if issues else 0

    done_issues = [i for i in issues
                   if i["fields"]["status"]["statusCategory"]["key"] == "done"]
    pct = round(len(done_issues) / total * 100, 1) if total else 0

    return {
        "epic": epic_key,
        "total_historias": total,
        "concluidas": len(done_issues),
        "percentual_conclusao": f"{pct}%",
        "story_points_total": total_pts,
        "story_points_concluidos": done_pts,
        "por_status": {k: len(v) for k, v in by_status.items()},
    }


# ---------------------------------------------------------------------------
# Modulo 4: Consulta Livre (JQL + resumo)
# ---------------------------------------------------------------------------

def free_query(jql: str, fields: str = "summary,status,assignee,priority,issuetype",
               max_results: int = 50):
    """Executa JQL livre e retorna lista estruturada de issues."""
    issues = _search(jql, fields, max_results=max_results)

    rows = []
    for issue in issues:
        f = issue["fields"]
        rows.append({
            "key": issue["key"],
            "resumo": f.get("summary", ""),
            "status": f.get("status", {}).get("name", ""),
            "responsavel": (f.get("assignee") or {}).get("displayName", "—"),
            "prioridade": (f.get("priority") or {}).get("name", "—"),
            "tipo": (f.get("issuetype") or {}).get("name", ""),
        })
    return {"total": len(rows), "issues": rows}
