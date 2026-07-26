"""
Jira Analytics — sprints, responsaveis, epicos e consultas livres.
Reutiliza JIRA_EMAIL + JIRA_API_TOKEN da jira-epic-automator.
"""
import os
import re
import sys
import subprocess
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("Instalando dependencia 'requests'...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests
from collections import defaultdict
from statistics import mean, stdev

JIRA_BASE   = "https://qx3prod.atlassian.net/rest/api/3"
JIRA_AGILE  = "https://qx3prod.atlassian.net/rest/agile/1.0"
JIRA_DEVINFO = "https://qx3prod.atlassian.net/rest/dev-status/latest"
PROJECTS   = ["TPROJ", "TNP", "TLIGHTDIST", "TLIGHTCOM", "THP",
              "TTRD", "TSRV", "PROJTHUN", "SUP", "TVAR"]

# Configuracao do usuario — carregada sob demanda
try:
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import config_manager as _cfg
except ImportError:
    _cfg = None


def resolve_board(name_or_id) -> int:
    """Resolve nome ou alias de board para ID numerico. Ex: 'projetos' -> 86."""
    if _cfg:
        bid = _cfg.resolve_board(name_or_id)
        if bid is not None:
            return bid
    try:
        return int(name_or_id)
    except (ValueError, TypeError):
        raise ValueError(f"Board '{name_or_id}' nao encontrado na configuracao do usuario.")


def default_board() -> int:
    """Retorna o board padrao configurado pelo usuario (fallback: 86)."""
    return _cfg.get_default_board() if _cfg else 86


def get_user_config() -> dict:
    """Retorna a configuracao completa do usuario."""
    return _cfg.load() if _cfg else {}


def update_user_config(action: str, **kwargs):
    """Atualiza a configuracao do usuario.

    Actions:
      add_board(alias, board_id)      — adiciona alias de board
      remove_board(alias)             — remove alias
      set_default_board(board_id)     — define board padrao
      add_developer(name)             — adiciona desenvolvedor monitorado
      remove_developer(name)          — remove desenvolvedor
      add_topic(topic)                — adiciona assunto de interesse
      remove_topic(topic)             — remove assunto
    """
    if not _cfg:
        return {"erro": "config_manager nao disponivel"}
    fn = getattr(_cfg, action, None)
    if not fn:
        return {"erro": f"Acao '{action}' desconhecida"}
    result = fn(**kwargs)
    return {"ok": True, "config": result}

# Status que indicam trabalho ativo (para calcular risco de atraso).
# "Em Testes" NAO esta aqui — significa desenvolvimento concluido neste contexto.
IN_PROGRESS_STATUSES = {"em andamento", "in progress", "doing", "em desenvolvimento",
                        "em analise", "em análise", "development", "in review"}


# ---------------------------------------------------------------------------
# Helpers de API
# ---------------------------------------------------------------------------

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
                      headers={"Accept": "application/json",
                               "Content-Type": "application/json"},
                      json=body)
    r.raise_for_status()
    return r.json() if r.content else {}


def _search(jql, fields, max_results=500):
    results, next_token = [], None
    fields_list = fields if isinstance(fields, list) else fields.split(",")
    while True:
        payload = {"jql": jql, "fields": fields_list,
                   "maxResults": min(100, max_results)}
        if next_token:
            payload["nextPageToken"] = next_token
        data = _post("/search/jql", payload)
        batch = data.get("issues", [])
        results.extend(batch)
        next_token = data.get("nextPageToken")
        if not next_token or len(results) >= max_results or len(batch) < 100:
            break
    return results


def _parse_dt(s):
    """Converte string ISO do Jira para datetime UTC-aware."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _hours(seconds):
    return round(seconds / 3600, 1) if seconds else 0


def _pts(val):
    """Extrai story points independente do tipo retornado."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return float(val)
    return 0


def _fmt_time(hours: float) -> str:
    """Formata horas como '48h (6d)' para facil leitura. Ex: 39.0 -> '39h (4.9d)'."""
    if not hours:
        return "—"
    return f"{round(hours)}h ({round(hours / 8, 1)}d)"


# ---------------------------------------------------------------------------
# Time in status + risco de atraso
# ---------------------------------------------------------------------------

def get_issue_time_in_status(issue_key: str) -> dict:
    """Retorna quanto tempo cada issue passou em cada status (via changelog).

    Retorna dict com:
      current_status, time_in_current_status_hours, time_in_current_status_days,
      status_history: [{status, start, end, hours}],
      original_estimate_hours, time_spent_hours, remaining_hours
    """
    data = _get(f"/issue/{issue_key}",
                params={"expand": "changelog",
                        "fields": "status,timeoriginalestimate,timespent,"
                                  "timeestimate,summary,assignee,issuetype"})
    f = data["fields"]
    current_status = f["status"]["name"]
    now = datetime.now(timezone.utc)

    # Reconstruir linha do tempo de status a partir do changelog
    histories = sorted(data["changelog"]["histories"],
                       key=lambda h: h["created"])

    # Encontrar a data de criacao como ponto inicial
    created_str = data.get("fields", {}).get("created") or histories[0]["created"] if histories else None
    # Se nao tiver created no fields, busca separado
    if not created_str:
        d2 = _get(f"/issue/{issue_key}", params={"fields": "created"})
        created_str = d2["fields"].get("created")

    # Montar lista de transicoes de status
    transitions = []
    for hist in histories:
        for item in hist["items"]:
            if item["field"] == "status":
                transitions.append({
                    "to": item["toString"],
                    "at": _parse_dt(hist["created"]),
                })

    # Calcular tempo por status
    status_history = []
    initial_status = "To Do"
    if transitions:
        # Antes da primeira transicao o issue estava no status inicial
        start = _parse_dt(created_str) if created_str else transitions[0]["at"]
        prev_status = initial_status
        for tr in transitions:
            end = tr["at"]
            if start and end and end > start:
                hours = (end - start).total_seconds() / 3600
                status_history.append({
                    "status": prev_status,
                    "start": start.isoformat()[:16],
                    "end": end.isoformat()[:16],
                    "hours": round(hours, 1),
                    "days": round(hours / 8, 1),  # dias uteis aproximados
                })
            prev_status = tr["to"]
            start = tr["at"]
        # Status atual (em aberto)
        if start:
            hours = (now - start).total_seconds() / 3600
            status_history.append({
                "status": prev_status,
                "start": start.isoformat()[:16],
                "end": None,
                "hours": round(hours, 1),
                "days": round(hours / 8, 1),
            })
    else:
        # Sem historico de transicoes — esta no status original desde a criacao
        start = _parse_dt(created_str) if created_str else now
        hours = (now - start).total_seconds() / 3600
        status_history.append({
            "status": current_status,
            "start": start.isoformat()[:16],
            "end": None,
            "hours": round(hours, 1),
            "days": round(hours / 8, 1),
        })

    current_entry = next((s for s in reversed(status_history)
                          if s["status"] == current_status and s["end"] is None), None)
    time_in_current = current_entry["hours"] if current_entry else 0

    orig_estimate_h = _hours(f.get("timeoriginalestimate") or 0)
    time_spent_h    = _hours(f.get("timespent") or 0)
    remaining_h     = _hours(f.get("timeestimate") or 0)

    return {
        "key": issue_key,
        "summary": f.get("summary", ""),
        "assignee": (f.get("assignee") or {}).get("displayName", "—"),
        "current_status": current_status,
        "time_in_current_status_hours": round(time_in_current, 1),
        "time_in_current_status_days": round(time_in_current / 8, 1),
        "original_estimate_hours": orig_estimate_h,
        "time_spent_hours": time_spent_h,
        "remaining_hours": remaining_h,
        "status_history": status_history,
    }


def calculate_delay_risk(time_data: dict) -> dict:
    """Calcula score de risco de atraso com base em tempo, estimativas e status.

    Niveis: BAIXO, MEDIO, ALTO, CRITICO, SEM_ESTIMATIVA.
    """
    status_norm = time_data["current_status"].lower()
    is_active = any(s in status_norm for s in IN_PROGRESS_STATUSES)

    if not is_active:
        return {"nivel": "N/A", "motivo": "Issue nao esta em status ativo"}

    orig  = time_data["original_estimate_hours"]
    spent = time_data["time_spent_hours"]
    rem   = time_data["remaining_hours"]
    days_in_status = time_data["time_in_current_status_days"]

    fatores = []

    if orig == 0:
        # Sem estimativa — risco por falta de transparencia
        if days_in_status >= 3:
            return {
                "nivel": "ALTO",
                "motivo": f"Sem estimativa original e {days_in_status:.1f} dias no status atual",
                "fatores": ["sem_estimativa", "tempo_sem_atualizacao"],
            }
        return {
            "nivel": "SEM_ESTIMATIVA",
            "motivo": "Estimativa original nao definida — impossivel prever atraso",
            "fatores": ["sem_estimativa"],
        }

    projected = spent + rem if rem > 0 else spent
    overrun_ratio = projected / orig if orig > 0 else 0

    # Fator 1: ja ultrapassou a estimativa
    if spent > orig:
        pct = round((spent / orig - 1) * 100)
        fatores.append(f"ja {pct}% acima da estimativa ({spent}h gasto vs {orig}h estimado)")

    # Fator 2: projecao de estouro
    elif overrun_ratio > 1:
        pct = round((overrun_ratio - 1) * 100)
        fatores.append(f"projecao {pct}% acima ({spent}h gasto + {rem}h restante vs {orig}h estimado)")

    # Fator 3: tempo no status atual vs estimativa total
    if orig > 0 and days_in_status > (orig / 8) * 0.8:
        fatores.append(f"{days_in_status:.1f} dias no status atual (estimativa total: {orig/8:.1f} dias)")

    # Fator 4: sem log de tempo (sem_transparencia)
    if orig > 0 and spent == 0 and days_in_status >= 1:
        fatores.append("nenhum tempo registrado — sem transparencia de progresso")

    # Classificar nivel
    if spent > orig * 1.5 or overrun_ratio > 1.5:
        nivel = "CRITICO"
    elif spent > orig or overrun_ratio > 1.2:
        nivel = "ALTO"
    elif overrun_ratio > 1.0 or (spent == 0 and days_in_status >= 2):
        nivel = "MEDIO"
    else:
        nivel = "BAIXO"

    return {
        "nivel": nivel,
        "overrun_ratio": round(overrun_ratio, 2),
        "original_estimate_h": orig,
        "spent_h": spent,
        "remaining_h": rem,
        "days_in_current_status": days_in_status,
        "fatores": fatores if fatores else ["dentro do prazo estimado"],
    }


# ---------------------------------------------------------------------------
# Modulo 1: Sprints
# ---------------------------------------------------------------------------

def list_boards(project_key=None):
    """Lista todos os boards acessiveis (com paginacao completa)."""
    all_boards, start = [], 0
    while True:
        params = {"maxResults": 50, "startAt": start}
        if project_key:
            params["projectKeyOrId"] = project_key
        data = _get("/board", params=params, base=JIRA_AGILE)
        values = data.get("values", [])
        all_boards.extend(values)
        if data.get("isLast", True) or not values:
            break
        start += len(values)
    return all_boards


def _get_active_sprint(board_id: int):
    """Retorna o sprint ativo de um board. Sempre prioriza o ativo."""
    sprints = _get(f"/board/{board_id}/sprint",
                   params={"state": "active"}, base=JIRA_AGILE)
    values = sprints.get("values", [])
    if values:
        return values[0]
    # Fallback: ultimo fechado
    closed = _get(f"/board/{board_id}/sprint",
                  params={"state": "closed", "maxResults": 10}, base=JIRA_AGILE)
    closed_vals = sorted(closed.get("values", []),
                         key=lambda s: s.get("endDate", ""), reverse=True)
    return closed_vals[0] if closed_vals else None


def _get_sprint_stories(board_id: int, sprint_id: int):
    """Retorna apenas historias (sem subtarefas) de um sprint."""
    issues = _get(
        f"/board/{board_id}/sprint/{sprint_id}/issue",
        params={"maxResults": 500,
                "fields": "summary,status,assignee,issuetype,priority,"
                          "timeoriginalestimate,timespent,timeestimate,"
                          "customfield_10016"},
        base=JIRA_AGILE
    ).get("issues", [])

    # Filtrar subtarefas
    return [i for i in issues if not i["fields"]["issuetype"].get("subtask", False)]


def get_sprint_report(board_id: int, sprint_id: int = None):
    """Metricas do sprint ativo (ou especifico se sprint_id informado).

    Inclui apenas historias — subtarefas sao excluidas.
    """
    sprint = (_get(f"/sprint/{sprint_id}", base=JIRA_AGILE)
              if sprint_id else _get_active_sprint(board_id))
    if not sprint:
        return {"erro": f"Nenhum sprint encontrado no board {board_id}"}

    sprint_id  = sprint["id"]
    start = sprint.get("startDate", "")[:10] if sprint.get("startDate") else "—"
    end   = sprint.get("endDate", "")[:10]   if sprint.get("endDate") else "—"

    stories = _get_sprint_stories(board_id, sprint_id)

    done, in_progress, todo, no_assignee = [], [], [], []
    total_pts = done_pts = 0

    for s in stories:
        f   = s["fields"]
        cat = f["status"]["statusCategory"]["key"]
        pts = _pts(f.get("customfield_10016"))
        total_pts += pts
        assignee = (f.get("assignee") or {}).get("displayName", "")
        if not assignee:
            no_assignee.append(s["key"])
        if cat == "done":
            done.append(s["key"]); done_pts += pts
        elif cat == "indeterminate":
            in_progress.append(s["key"])
        else:
            todo.append(s["key"])

    total = len(stories)
    taxa  = round(len(done) / total * 100, 1) if total else 0

    return {
        "sprint": sprint.get("name"),
        "estado": sprint.get("state"),
        "periodo": f"{start} a {end}",
        "total_historias": total,
        "concluidas": len(done),
        "em_andamento": len(in_progress),
        "nao_iniciadas": len(todo),
        "taxa_conclusao": f"{taxa}%",
        "story_points_total": total_pts,
        "story_points_concluidos": done_pts,
        "sem_responsavel": no_assignee,
        "nao_concluidas": in_progress + todo,
        "keys_em_andamento": in_progress,
        "keys_nao_iniciadas": todo,
    }


def _get_subtask_aggregate(story_key: str) -> dict:
    """Agrega tempo de todas as subtarefas de uma historia.

    O time registra horas nas subtarefas, nao na historia pai.
    Retorna totais de estimativa/gasto/restante e lista de subtarefas com tempo excedido.
    """
    subtasks = _search(
        f"parent = {story_key}",
        "summary,status,timeoriginalestimate,timespent,timeestimate",
        max_results=100
    )

    total_orig = total_spent = total_rem = 0
    overdue = []

    for st in subtasks:
        f    = st["fields"]
        orig  = _hours(f.get("timeoriginalestimate") or 0)
        spent = _hours(f.get("timespent") or 0)
        rem   = _hours(f.get("timeestimate") or 0)
        cat   = f["status"]["statusCategory"]["key"]

        total_orig  += orig
        total_spent += spent
        total_rem   += rem

        # Subtarefa que ultrapassou a estimativa
        if orig > 0 and spent > orig * 1.2:
            overrun_pct = round((spent / orig - 1) * 100)
            overdue.append({
                "key":        st["key"],
                "summary":    f.get("summary", "")[:60],
                "status":     f["status"]["name"],
                "original_h": orig,
                "spent_h":    spent,
                "overrun_pct": overrun_pct,
            })
        # Subtarefa ativa sem nenhum apontamento (falta de transparencia)
        elif cat == "indeterminate" and spent == 0 and orig > 0:
            overdue.append({
                "key":        st["key"],
                "summary":    f.get("summary", "")[:60],
                "status":     f["status"]["name"],
                "original_h": orig,
                "spent_h":    0,
                "overrun_pct": None,
                "aviso":      "sem apontamento",
            })

    return {
        "total_subtasks":    len(subtasks),
        "original_estimate_h": round(total_orig, 1),
        "time_spent_h":      round(total_spent, 1),
        "remaining_h":       round(total_rem, 1),
        "overdue_subtasks":  overdue,
    }


def get_sprint_stories_detail(board_id: int, sprint_id: int = None,
                               status_filter: str = None):
    """Retorna historias do sprint com time-in-status e risco de atraso.

    status_filter: 'in_progress', 'todo', 'done' ou None (todas)
    """
    sprint = (_get(f"/sprint/{sprint_id}", base=JIRA_AGILE)
              if sprint_id else _get_active_sprint(board_id))
    if not sprint:
        return []

    stories = _get_sprint_stories(board_id, sprint["id"])

    cat_map = {"in_progress": "indeterminate", "done": "done", "todo": "new"}
    if status_filter and status_filter in cat_map:
        target_cat = cat_map[status_filter]
        stories = [s for s in stories
                   if s["fields"]["status"]["statusCategory"]["key"] == target_cat]

    results = []
    for s in stories:
        f        = s["fields"]
        assignee = (f.get("assignee") or {}).get("displayName", "Sem responsavel")
        status   = f["status"]["name"]

        time_data = get_issue_time_in_status(s["key"])

        # Substituir estimativas da historia pelo agregado das subtarefas,
        # pois o time aponta horas nas subtarefas — nao na historia pai.
        sub_agg = _get_subtask_aggregate(s["key"])
        if sub_agg["total_subtasks"] > 0:
            time_data["original_estimate_hours"] = sub_agg["original_estimate_h"]
            time_data["time_spent_hours"]         = sub_agg["time_spent_h"]
            time_data["remaining_hours"]           = sub_agg["remaining_h"]

        risk = calculate_delay_risk(time_data)

        # Se ha subtarefas com tempo excedido, o risco minimo e MEDIO
        if sub_agg["overdue_subtasks"] and risk["nivel"] in ("N/A", "SEM_ESTIMATIVA", "BAIXO"):
            risk = {
                "nivel": "MEDIO",
                "fatores": [f"subtarefa {st['key']} com tempo excedido ou sem apontamento"
                            for st in sub_agg["overdue_subtasks"]],
            }

        # Label amigavel para Em Testes
        status_label = "Em Testes (dev concluido)" if status.lower() == "em testes" else status

        hours_in_status = time_data["time_in_current_status_days"] * 8
        orig_h  = sub_agg["original_estimate_h"] if sub_agg["total_subtasks"] > 0 \
                  else time_data["original_estimate_hours"]
        spent_h = sub_agg["time_spent_h"] if sub_agg["total_subtasks"] > 0 \
                  else time_data["time_spent_hours"]
        rem_h   = sub_agg["remaining_h"] if sub_agg["total_subtasks"] > 0 \
                  else time_data["remaining_hours"]

        # Overrun da historia: (gasto + restante) vs estimativa original
        if orig_h > 0 and (spent_h + rem_h) > 0:
            proj = spent_h + rem_h if rem_h > 0 else spent_h
            overrun_pct = round((proj / orig_h - 1) * 100)
            story_overrun = f"+{overrun_pct}%" if overrun_pct > 0 else (
                f"{overrun_pct}%" if overrun_pct < 0 else "0%"
            )
        else:
            story_overrun = "—"

        results.append({
            "key":               s["key"],
            "summary":           f.get("summary", "")[:80],
            "status":            status_label,
            "assignee":          assignee,
            "subtasks_total":    sub_agg["total_subtasks"],
            # Tempo no status atual formatado
            "tempo_no_status":   _fmt_time(hours_in_status),
            "tempo_no_status_h": round(hours_in_status, 1),
            "tempo_no_status_d": time_data["time_in_current_status_days"],
            # Estimativas formatadas
            "estimativa_original": _fmt_time(orig_h),
            "gasto":             _fmt_time(spent_h),
            "restante":          _fmt_time(rem_h),
            "overrun":           story_overrun,
            # Risco
            "risk":              risk["nivel"],
            "risk_detail":       risk.get("fatores", []),
            "subtasks_overdue":  sub_agg["overdue_subtasks"],
            "status_history":    time_data["status_history"],
        })

    return sorted(results, key=lambda x: (
        ["CRITICO", "ALTO", "MEDIO", "BAIXO", "SEM_ESTIMATIVA", "N/A"].index(
            x["risk"] if x["risk"] in ["CRITICO", "ALTO", "MEDIO", "BAIXO",
                                        "SEM_ESTIMATIVA", "N/A"] else "N/A"
        )
    ))


def get_sprint_velocity(board_id: int, num_sprints: int = 5):
    """Velocidade dos ultimos N sprints fechados (historias e story points)."""
    data = _get(f"/board/{board_id}/sprint",
                params={"state": "closed", "maxResults": num_sprints * 2},
                base=JIRA_AGILE)
    sprints = sorted(data.get("values", []),
                     key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]

    velocities = []
    for sprint in sprints:
        stories = _get_sprint_stories(board_id, sprint["id"])
        done_stories = [s for s in stories
                        if s["fields"]["status"]["statusCategory"]["key"] == "done"]
        pts = sum(_pts(s["fields"].get("customfield_10016")) for s in done_stories)
        velocities.append({
            "sprint": sprint.get("name"),
            "periodo": sprint.get("endDate", "")[:10],
            "historias_concluidas": len(done_stories),
            "total_historias": len(stories),
            "taxa_conclusao": f"{round(len(done_stories)/len(stories)*100,1)}%" if stories else "0%",
            "story_points": pts,
        })

    avg_stories = round(mean(v["historias_concluidas"] for v in velocities), 1) if velocities else 0
    avg_pts     = round(mean(v["story_points"] for v in velocities), 1) if velocities else 0

    return {
        "board_id": board_id,
        "sprints_analisados": len(velocities),
        "media_historias_por_sprint": avg_stories,
        "media_story_points": avg_pts,
        "historico": velocities,
    }


# ---------------------------------------------------------------------------
# Modulo 2: Responsaveis — metricas por desenvolvedor
# ---------------------------------------------------------------------------

def _get_sprint_subtask_time(story_keys: list) -> dict:
    """Agrega tempo de subtarefas para um conjunto de historias em uma unica chamada JQL.

    Retorna {story_key: {original_h, spent_h, remaining_h}}
    """
    if not story_keys:
        return {}
    keys_str = ",".join(story_keys)
    subtasks = _search(
        f"parent in ({keys_str})",
        "parent,timeoriginalestimate,timespent,timeestimate",
        max_results=500
    )
    by_parent = defaultdict(lambda: {"original_h": 0.0, "spent_h": 0.0, "remaining_h": 0.0})
    for st in subtasks:
        f = st["fields"]
        parent_key = (f.get("parent") or {}).get("key")
        if parent_key:
            by_parent[parent_key]["original_h"]  += _hours(f.get("timeoriginalestimate") or 0)
            by_parent[parent_key]["spent_h"]      += _hours(f.get("timespent") or 0)
            by_parent[parent_key]["remaining_h"]  += _hours(f.get("timeestimate") or 0)
    return dict(by_parent)


def get_developer_deep_analysis(board_id: int, num_sprints: int = 6):
    """Analise aprofundada por desenvolvedor: entrega, estimativas, lead time e tendencia.

    Coleta dados individuais por historia (via subtarefas para tempo real).
    Considera apenas devs presentes em pelo menos 2 sprints (time recorrente).

    Metricas:
    - Taxa de entrega e variacao (consistencia)
    - Throughput medio (historias/sprint)
    - Tamanho medio das historias assumidas (estimativa original)
    - Lead time medio por historia concluida (horas efetivas gastas)
    - MAPE: desvio medio entre tempo gasto e estimativa nas historias concluidas
    - Taxa de overrun: % de historias que excederam a estimativa em >20%
    - Tendencia: melhora/estabilidade/piora entre primeiros e ultimos sprints
    - Confianca de entrega e precisao de estimativa classificadas
    """
    data = _get(f"/board/{board_id}/sprint",
                params={"state": "closed", "maxResults": num_sprints * 2},
                base=JIRA_AGILE)
    sprints = sorted(data.get("values", []),
                     key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]
    sprints = list(reversed(sprints))  # ordem cronologica: mais antigo primeiro

    dev_sprints = defaultdict(list)

    for sprint in sprints:
        stories = _get_sprint_stories(board_id, sprint["id"])
        if not stories:
            continue

        # Uma chamada JQL para todas as subtarefas do sprint
        story_keys = [s["key"] for s in stories]
        sub_time   = _get_sprint_subtask_time(story_keys)

        by_dev = defaultdict(lambda: {
            "committed": 0, "done": 0,
            "story_sizes_h": [], "spent_per_story_h": [],
            "mape_samples": [], "overrun_count": 0, "no_estimate": 0,
        })

        for s in stories:
            f        = s["fields"]
            assignee = (f.get("assignee") or {}).get("displayName")
            if not assignee:
                continue
            cat = f["status"]["statusCategory"]["key"]

            # Tempo: subtarefas tem prioridade; fallback para campos diretos
            sub   = sub_time.get(s["key"], {})
            orig  = sub.get("original_h") or _hours(f.get("timeoriginalestimate") or 0)
            spent = sub.get("spent_h")    or _hours(f.get("timespent") or 0)

            d = by_dev[assignee]
            d["committed"] += 1
            if orig > 0:
                d["story_sizes_h"].append(orig)
            else:
                d["no_estimate"] += 1

            if cat == "done":
                d["done"] += 1
                if spent > 0:
                    d["spent_per_story_h"].append(spent)
                    if orig > 0:
                        d["mape_samples"].append(
                            abs(spent - orig) / orig * 100
                        )
                if orig > 0 and spent > orig * 1.2:
                    d["overrun_count"] += 1

        for assignee, d in by_dev.items():
            rate     = round(d["done"] / d["committed"] * 100, 1) if d["committed"] else 0
            avg_size = round(mean(d["story_sizes_h"]), 1) if d["story_sizes_h"] else None
            avg_lead = round(mean(d["spent_per_story_h"]), 1) if d["spent_per_story_h"] else None
            avg_mape = round(mean(d["mape_samples"]), 1) if d["mape_samples"] else None

            dev_sprints[assignee].append({
                "sprint":     sprint.get("name"),
                "end_date":   sprint.get("endDate", "")[:10],
                "committed":  d["committed"],
                "done":       d["done"],
                "delivery_rate":    rate,
                "avg_story_size_h": avg_size,
                "avg_lead_time_h":  avg_lead,
                "sprint_mape":      avg_mape,
                "overrun_count":    d["overrun_count"],
                "no_estimate":      d["no_estimate"],
            })

    results = {}
    for dev, sprint_list in dev_sprints.items():
        if len(sprint_list) < 2:
            continue

        rates      = [s["delivery_rate"] for s in sprint_list]
        throughputs = [s["done"]         for s in sprint_list]
        sizes  = [s["avg_story_size_h"]  for s in sprint_list if s["avg_story_size_h"]]
        leads  = [s["avg_lead_time_h"]   for s in sprint_list if s["avg_lead_time_h"]]
        mapes  = [s["sprint_mape"]       for s in sprint_list if s["sprint_mape"] is not None]

        total_done    = sum(s["done"]           for s in sprint_list)
        total_overrun = sum(s["overrun_count"]  for s in sprint_list)
        total_no_est  = sum(s["no_estimate"]    for s in sprint_list)
        total_committed = sum(s["committed"]    for s in sprint_list)

        avg_rate  = round(mean(rates), 1)
        std_rate  = round(stdev(rates), 1) if len(rates) > 1 else 0
        avg_tput  = round(mean(throughputs), 1)
        std_tput  = round(stdev(throughputs), 1) if len(throughputs) > 1 else 0
        avg_size  = round(mean(sizes), 1) if sizes else None
        avg_lead  = round(mean(leads), 1) if leads else None
        avg_mape  = round(mean(mapes), 1) if mapes else None
        overrun_rate = round(total_overrun / total_done * 100, 1) if total_done else None
        no_est_rate  = round(total_no_est / total_committed * 100, 1) if total_committed else None

        # Confianca de entrega
        if avg_rate >= 85 and std_rate <= 10:
            confianca = "ALTA"
        elif avg_rate >= 70 and std_rate <= 20:
            confianca = "MEDIA"
        elif avg_rate >= 50:
            confianca = "BAIXA"
        else:
            confianca = "CRITICA"

        # Precisao de estimativa
        if avg_mape is None:
            precisao = "SEM_DADOS"
        elif avg_mape <= 20:
            precisao = "PRECISO"
        elif avg_mape <= 40:
            precisao = "ACEITAVEL"
        elif avg_mape <= 70:
            precisao = "IMPRECISO"
        else:
            precisao = "MUITO_IMPRECISO"

        # Tendencia: primeiro terco vs ultimo terco dos sprints
        n   = len(sprint_list)
        cut = max(1, n // 3)
        early_avg  = mean([s["delivery_rate"] for s in sprint_list[:cut]])
        recent_avg = mean([s["delivery_rate"] for s in sprint_list[-cut:]])
        delta = round(recent_avg - early_avg, 1)
        if delta >= 10:
            trend = f"MELHORANDO (+{delta}%)"
        elif delta <= -10:
            trend = f"PIORANDO ({delta}%)"
        else:
            trend = f"ESTAVEL ({'+' if delta >= 0 else ''}{delta}%)"

        results[dev] = {
            "sprints_analisados":       len(sprint_list),
            "throughput_medio":         f"{avg_tput} hist/sprint (±{std_tput})",
            "taxa_entrega_media":       f"{avg_rate}%",
            "variacao_entrega":         f"±{std_rate}%",
            "confianca_entrega":        confianca,
            "tendencia":                trend,
            "tamanho_medio_historia":   _fmt_time(avg_size) if avg_size else "—",
            "lead_time_medio":          _fmt_time(avg_lead) if avg_lead else "—",
            "erro_estimativa_mape":     f"{avg_mape}%" if avg_mape is not None else "—",
            "precisao_estimativa":      precisao,
            "taxa_overrun":             f"{overrun_rate}%" if overrun_rate is not None else "—",
            "sem_estimativa":           f"{no_est_rate}%" if no_est_rate is not None else "—",
            "historico_sprints":        sprint_list,
        }

    order = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2, "CRITICA": 3}
    return dict(sorted(
        results.items(),
        key=lambda x: (order.get(x[1]["confianca_entrega"], 4),
                       -float(x[1]["taxa_entrega_media"].replace("%", "") or 0))
    ))


def _get_developer_sprint_data(board_id: int, sprint) -> dict:
    """Extrai dados de um sprint por desenvolvedor (historias, conclusao, estimativas)."""
    stories   = _get_sprint_stories(board_id, sprint["id"])
    by_dev    = defaultdict(lambda: {
        "committed": 0, "done": 0,
        "estimated_h": 0, "spent_h": 0,
        "cycle_times_h": [],
    })

    for s in stories:
        f        = s["fields"]
        assignee = (f.get("assignee") or {}).get("displayName")
        if not assignee:
            continue
        cat = f["status"]["statusCategory"]["key"]
        by_dev[assignee]["committed"] += 1
        by_dev[assignee]["estimated_h"] += _hours(f.get("timeoriginalestimate") or 0)
        by_dev[assignee]["spent_h"]     += _hours(f.get("timespent") or 0)

        if cat == "done":
            by_dev[assignee]["done"] += 1

    return dict(by_dev)


def get_developer_metrics(board_id: int, num_sprints: int = 5):
    """Metricas ageis por desenvolvedor com base nos ultimos N sprints fechados.

    Metricas calculadas:
    - Taxa de entrega (delivery rate): % historias concluidas vs comprometidas
    - Confianca media: estabilidade da taxa de entrega ao longo dos sprints
    - Erro de estimativa (MAPE): desvio medio entre estimativa e tempo real
    - Throughput medio: historias entregues por sprint
    - Ciclo medio: sera calculado se solicitado com detalhamento por issue
    """
    data = _get(f"/board/{board_id}/sprint",
                params={"state": "closed", "maxResults": num_sprints * 2},
                base=JIRA_AGILE)
    sprints = sorted(data.get("values", []),
                     key=lambda s: s.get("endDate", ""), reverse=True)[:num_sprints]

    # Acumular dados por desenvolvedor por sprint
    dev_sprints = defaultdict(list)
    for sprint in sprints:
        sprint_data = _get_developer_sprint_data(board_id, sprint)
        for dev, metrics in sprint_data.items():
            dev_sprints[dev].append({
                "sprint": sprint.get("name"),
                **metrics,
            })

    results = {}
    for dev, sprint_list in dev_sprints.items():
        delivery_rates = []
        estimation_errors = []
        throughputs = []

        for sp in sprint_list:
            # Taxa de entrega
            if sp["committed"] > 0:
                rate = sp["done"] / sp["committed"] * 100
                delivery_rates.append(rate)
                throughputs.append(sp["done"])

            # Erro de estimativa (MAPE) — so quando ha estimativa E tempo registrado
            if sp["estimated_h"] > 0 and sp["spent_h"] > 0:
                mape = abs(sp["spent_h"] - sp["estimated_h"]) / sp["estimated_h"] * 100
                estimation_errors.append(mape)

        avg_delivery   = round(mean(delivery_rates), 1)    if delivery_rates   else None
        std_delivery   = round(stdev(delivery_rates), 1)   if len(delivery_rates) > 1 else 0
        avg_mape       = round(mean(estimation_errors), 1) if estimation_errors else None
        avg_throughput = round(mean(throughputs), 1)       if throughputs       else 0

        # Nivel de confianca baseado na taxa media e consistencia
        if avg_delivery is None:
            confianca = "SEM_DADOS"
        elif avg_delivery >= 85 and std_delivery <= 10:
            confianca = "ALTA"
        elif avg_delivery >= 70 and std_delivery <= 20:
            confianca = "MEDIA"
        elif avg_delivery >= 50:
            confianca = "BAIXA"
        else:
            confianca = "CRITICA"

        # Classificacao do erro de estimativa
        if avg_mape is None:
            estimativa_label = "SEM_DADOS"
        elif avg_mape <= 20:
            estimativa_label = "PRECISO"
        elif avg_mape <= 40:
            estimativa_label = "ACEITAVEL"
        elif avg_mape <= 70:
            estimativa_label = "IMPRECISO"
        else:
            estimativa_label = "MUITO_IMPRECISO"

        results[dev] = {
            "sprints_analisados": len(sprint_list),
            "taxa_entrega_media": f"{avg_delivery}%" if avg_delivery is not None else "—",
            "variacao_entrega": f"±{std_delivery}%" if std_delivery else "—",
            "confianca_entrega": confianca,
            "throughput_medio": avg_throughput,
            "erro_estimativa_mape": f"{avg_mape}%" if avg_mape is not None else "—",
            "precisao_estimativa": estimativa_label,
            "historico_sprints": sprint_list,
        }

    # Ordenar por taxa de entrega (melhor primeiro)
    return dict(sorted(
        results.items(),
        key=lambda x: float(x[1]["taxa_entrega_media"].replace("%","") or 0)
                      if x[1]["taxa_entrega_media"] != "—" else -1,
        reverse=True
    ))


def get_assignee_workload(project_keys=None):
    """Carga atual de todos os responsaveis nos sprints abertos."""
    projects = project_keys or PROJECTS
    jql = (
        f'project in ({",".join(projects)}) '
        f'AND issuetype in standardIssueTypes() '
        f'AND issuetype not in subTaskIssueTypes() '
        f'AND sprint in openSprints() '
        f'AND assignee is not EMPTY'
    )
    issues = _search(jql, "summary,status,assignee,customfield_10016,priority")

    workload = defaultdict(lambda: {"total": 0, "done": 0, "in_progress": 0,
                                    "todo": 0, "points": 0, "issues": []})
    for issue in issues:
        f        = issue["fields"]
        assignee = (f.get("assignee") or {}).get("displayName", "Desconhecido")
        cat      = f["status"]["statusCategory"]["key"]
        pts      = _pts(f.get("customfield_10016"))
        workload[assignee]["total"]  += 1
        workload[assignee]["points"] += pts
        workload[assignee]["issues"].append(issue["key"])
        if cat == "done":
            workload[assignee]["done"] += 1
        elif cat == "indeterminate":
            workload[assignee]["in_progress"] += 1
        else:
            workload[assignee]["todo"] += 1

    return dict(sorted(workload.items(),
                       key=lambda x: x[1]["total"], reverse=True))


def get_assignee_productivity(assignee_query: str, days: int = 30):
    """Produtividade individual nos ultimos N dias."""
    since = (date.today() - timedelta(days=days)).isoformat()
    users = requests.get(
        f"{JIRA_BASE}/user/search", auth=_auth(),
        headers={"Accept": "application/json"},
        params={"query": assignee_query, "maxResults": 5}
    ).json()
    if not users:
        return {"erro": f"Nenhum usuario encontrado para '{assignee_query}'"}
    user = users[0]
    account_id   = user["accountId"]
    display_name = user["displayName"]

    done_issues = _search(
        f'assignee = "{account_id}" AND resolutiondate >= "{since}" '
        f'AND statusCategory = Done '
        f'AND issuetype in standardIssueTypes() '
        f'AND issuetype not in subTaskIssueTypes()',
        "summary,resolutiondate,customfield_10016,priority,issuetype"
    )
    open_issues = _search(
        f'assignee = "{account_id}" AND statusCategory != Done '
        f'AND issuetype in standardIssueTypes() '
        f'AND issuetype not in subTaskIssueTypes()',
        "summary,status,customfield_10016,priority,issuetype"
    )
    done_pts = sum(_pts(i["fields"].get("customfield_10016")) for i in done_issues)
    open_pts = sum(_pts(i["fields"].get("customfield_10016")) for i in open_issues)

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
    """Diagnostico de epicos ativos: campos faltando, garantia vencida, riscos."""
    from pathlib import Path
    import json

    projects     = project_keys or PROJECTS
    fields_cache = Path.home() / ".claude" / "jira-epic-automator-fields.json"
    custom       = {}
    if fields_cache.exists():
        with open(fields_cache) as fp:
            custom = json.load(fp)

    pub_fid     = custom.get("field_publication_date", "customfield_11336")
    warranty_fid = custom.get("field_warranty_date", "customfield_12167")
    quarter_fid = custom.get("field_quarter", "customfield_11450")
    start_fid   = custom.get("field_start_date", "customfield_11201")

    excluded = '"Em Producao", "Em Produção", "Concluido", "Concluído", "Done", "Fechado", "Closed"'
    jql      = (f'issuetype = Epic AND project in ({",".join(projects)}) '
                f'AND status not in ({excluded})')
    fields   = f"summary,status,assignee,{pub_fid},{warranty_fid},{quarter_fid},{start_fid},fixVersions"
    epics    = _search(jql, fields, max_results=300)

    today  = date.today()
    report = {
        "total": len(epics),
        "sem_responsavel": [], "sem_quarter": [],
        "sem_start_date": [], "sem_data_publicacao": [],
        "garantia_vencida": [], "em_risco": [], "saudaveis": [],
    }

    for epic in epics:
        f      = epic["fields"]
        key    = epic["key"]
        issues = []
        if not f.get("assignee"):
            issues.append("sem responsavel"); report["sem_responsavel"].append(key)
        if not f.get(quarter_fid):
            issues.append("sem quarter");     report["sem_quarter"].append(key)
        if not f.get(start_fid):
            issues.append("sem start date");  report["sem_start_date"].append(key)
        if not f.get(pub_fid):
            issues.append("sem data de publicacao"); report["sem_data_publicacao"].append(key)
        warranty_str = f.get(warranty_fid)
        if warranty_str:
            if today > date.fromisoformat(warranty_str[:10]):
                issues.append(f"garantia vencida em {warranty_str[:10]}")
                report["garantia_vencida"].append(key)
        if issues:
            report["em_risco"].append({
                "key": key,
                "summary": f.get("summary", ""),
                "issues": issues
            })
        else:
            report["saudaveis"].append(key)

    return report


def get_epic_progress(epic_key: str):
    """Progresso detalhado de um epico: historias por status e % conclusao."""
    stories = _search(
        f'"Epic Link" = {epic_key} OR parent = {epic_key}',
        "summary,status,assignee,customfield_10016,issuetype",
        max_results=200
    )
    # Separar historias de subtarefas
    stories_only   = [s for s in stories if not s["fields"]["issuetype"].get("subtask", False)]
    subtasks_only  = [s for s in stories if s["fields"]["issuetype"].get("subtask", False)]

    if not stories_only:
        return {"epic": epic_key, "historias": 0, "subtarefas": len(subtasks_only),
                "aviso": "Nenhuma historia encontrada"}

    by_status     = defaultdict(list)
    done_issues   = []
    total_pts = done_pts = 0

    for s in stories_only:
        f      = s["fields"]
        status = f["status"]["name"]
        cat    = f["status"]["statusCategory"]["key"]
        pts    = _pts(f.get("customfield_10016"))
        total_pts += pts
        by_status[status].append(s["key"])
        if cat == "done":
            done_issues.append(s["key"]); done_pts += pts

    total = len(stories_only)
    pct   = round(len(done_issues) / total * 100, 1) if total else 0

    return {
        "epic": epic_key,
        "total_historias": total,
        "total_subtarefas": len(subtasks_only),
        "concluidas": len(done_issues),
        "percentual_conclusao": f"{pct}%",
        "story_points_total": total_pts,
        "story_points_concluidos": done_pts,
        "por_status": {k: len(v) for k, v in by_status.items()},
    }


# ---------------------------------------------------------------------------
# Modulo 4: Consulta Livre
# ---------------------------------------------------------------------------

def free_query(jql: str,
               fields: str = "summary,status,assignee,priority,issuetype",
               max_results: int = 50):
    """Executa qualquer JQL e retorna resultado estruturado."""
    issues = _search(jql, fields, max_results=max_results)
    rows   = []
    for issue in issues:
        f = issue["fields"]
        rows.append({
            "key":        issue["key"],
            "resumo":     f.get("summary", ""),
            "status":     f.get("status", {}).get("name", ""),
            "responsavel": (f.get("assignee") or {}).get("displayName", "—"),
            "prioridade": (f.get("priority") or {}).get("name", "—"),
            "tipo":       (f.get("issuetype") or {}).get("name", ""),
        })
    return {"total": len(rows), "issues": rows}


# ---------------------------------------------------------------------------
# Modulo 5: Pesquisa Avancada de Conteudo
# ---------------------------------------------------------------------------

_STOPWORDS = {
    # Portugues
    "a", "o", "as", "os", "um", "uma", "uns", "umas",
    "de", "do", "da", "dos", "das", "du", "no", "na", "nos", "nas",
    "ao", "aos", "as", "pelo", "pela", "pelos", "pelas",
    "e", "ou", "mas", "nem", "pois", "que", "se", "para",
    "com", "sem", "em", "por", "ate", "ate", "sob",
    "ele", "ela", "eles", "elas", "eu", "tu", "nos", "vos",
    "esse", "essa", "esses", "essas", "este", "esta", "estes", "estas",
    "aquele", "aquela", "aqueles", "aquelas", "isso", "isto", "aquilo",
    "seu", "sua", "seus", "suas", "meu", "minha", "meus", "minhas",
    "foi", "ser", "ter", "tem", "sao", "esta", "estao", "tinha",
    "como", "quando", "onde", "qual", "quais", "quem",
    "sobre", "mais", "menos", "muito", "pouco", "bem", "mal",
    "pode", "deve", "vai", "ira", "devo", "posso",
    "sim", "nao", "ja", "ainda", "sempre", "nunca",
    "me", "te", "lhe", "lhes", "nos", "vos",
    "ha", "ate", "so", "tudo", "todo", "toda", "todos", "todas",
    "outro", "outra", "outros", "outras", "mesmo", "mesma",
    # Ingles
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "in", "on", "at", "to", "of", "for", "and", "or", "not", "it",
    "this", "that", "these", "those", "with", "from", "by", "as",
    "have", "has", "had", "will", "would", "could", "should",
    "its", "their", "our", "your", "my", "his", "her",
}

# Sinonimos/expansoes para termos de contexto agil
_TERM_EXPANSIONS = {
    "criterio": ["criterio", "aceite", "acceptance", "AC:"],
    "aceite":   ["aceite", "criterio", "acceptance"],
    "cenario":  ["cenario", "scenario", "dado que", "quando", "entao", "gherkin"],
    "teste":    ["teste", "test", "cenario", "validacao", "verificacao"],
    "combinado": ["combinado", "acordado", "decisao", "ficou", "definido", "alinhado"],
    "decisao":  ["decisao", "decidido", "combinado", "acordado", "definido"],
    "bug":      ["bug", "erro", "falha", "problema", "issue", "defeito"],
    "regra":    ["regra", "negocio", "requisito", "rule", "logica"],
}


def _adf_to_text(node) -> str:
    """Converte Atlassian Document Format (ADF) para texto plano pesquisavel."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(filter(None, (_adf_to_text(c) for c in node)))
    if not isinstance(node, dict):
        return ""

    ntype   = node.get("type", "")
    text    = node.get("text", "")
    content = node.get("content", [])

    if text:
        return text

    if ntype in ("doc", "blockquote"):
        return "\n\n".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype in ("paragraph", "heading"):
        return " ".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype in ("bulletList", "orderedList"):
        return "\n".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype == "listItem":
        return "- " + " ".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype in ("strong", "em", "code", "strike", "underline"):
        return "".join(_adf_to_text(c) for c in content)
    if ntype == "link":
        label = "".join(_adf_to_text(c) for c in content)
        return label or node.get("attrs", {}).get("href", "")
    if ntype == "hardBreak":
        return "\n"
    if ntype == "rule":
        return "\n---\n"
    if ntype == "codeBlock":
        return "".join(_adf_to_text(c) for c in content)
    if ntype == "table":
        return "\n".join(
            " | ".join(
                " ".join(filter(None, (_adf_to_text(cell) for cell in row.get("content", []))))
                for row in content
            )
        )
    if ntype in ("tableRow",):
        return " | ".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype in ("tableCell", "tableHeader"):
        return " ".join(filter(None, (_adf_to_text(c) for c in content)))
    if ntype == "mention":
        return node.get("attrs", {}).get("text", "@mencionado")
    if ntype == "emoji":
        return node.get("attrs", {}).get("shortName", "")
    if ntype == "inlineCard":
        return "[" + node.get("attrs", {}).get("url", "link") + "]"
    if ntype == "mediaInline":
        return "[media]"

    return " ".join(filter(None, (_adf_to_text(c) for c in content)))


def _extract_snippet(text: str, terms: list, context: int = 200) -> str:
    """Extrai trecho contextualizado ao redor da primeira ocorrencia de qualquer termo."""
    text_l = text.lower()
    best_idx = -1

    # Prioriza termos mais longos (mais especificos)
    for term in sorted(terms, key=len, reverse=True):
        idx = text_l.find(term.lower())
        if idx >= 0:
            best_idx = idx
            break

    if best_idx < 0:
        return (text[:context] + "…") if len(text) > context else text

    start = max(0, best_idx - context // 2)
    end   = min(len(text), best_idx + context // 2)

    # Ajustar para nao cortar no meio de palavras
    if start > 0:
        start = text.rfind(" ", 0, start) + 1
    if end < len(text):
        end = text.find(" ", end)
        if end < 0:
            end = len(text)

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _parse_natural_query(query: str) -> dict:
    """Extrai termos e metadados de uma query em linguagem natural (portugues/ingles).

    Retorna:
      terms: lista de termos para busca
      phrases: frases exatas entre aspas
      days_filter: int ou None
      field_hints: campos sugeridos com base em palavras-chave
    """
    # Extrair frases exatas entre aspas
    phrases = re.findall(r'"([^"]+)"', query)
    clean   = re.sub(r'"[^"]+"', " ", query)

    # Detectar filtro de data na query
    days_filter = None
    m = re.search(r'(?:ultim[oa]s?\s+)?(\d+)\s+dias?', clean, re.I)
    if m:
        days_filter = int(m.group(1))
        clean = clean[:m.start()] + clean[m.end():]
    elif re.search(r'esta\s+semana', clean, re.I):
        days_filter = 7
        clean = re.sub(r'esta\s+semana', " ", clean, flags=re.I)
    elif re.search(r'este\s+m[eê]s', clean, re.I):
        days_filter = 30
        clean = re.sub(r'este\s+m[eê]s', " ", clean, flags=re.I)

    # Field hints
    field_hints = []
    q_lower = clean.lower()
    if any(w in q_lower for w in ["comentario", "comentários", "combinado", "acordado",
                                   "decisao", "comment", "alinhado", "definido"]):
        field_hints.append("comment")
    if any(w in q_lower for w in ["criterio", "aceite", "acceptance", "ac:", "descricao",
                                   "cenario", "scenario", "gherkin", "dado que"]):
        field_hints.append("description")

    # Tokenizar e remover stopwords
    tokens = re.sub(r"[^\w\sáéíóúâêôãõàçüñ]", " ", clean, flags=re.U).split()
    original_terms = []
    expanded_terms = []

    for t in tokens:
        t_norm = t.lower().strip()
        if len(t_norm) >= 3 and t_norm not in _STOPWORDS:
            if t_norm not in original_terms:
                original_terms.append(t_norm)
            # Coletar expansoes separadamente (usadas apenas para ranking local)
            for key, expansions in _TERM_EXPANSIONS.items():
                if t_norm == key or t_norm.startswith(key[:5]):
                    for e in expansions:
                        if e not in original_terms and e not in expanded_terms:
                            expanded_terms.append(e)
                    break

    # Frases exatas entram como termos originais prioritarios
    for phrase in phrases:
        if phrase not in original_terms:
            original_terms.insert(0, phrase)

    return {
        "terms":         original_terms,         # usados no JQL
        "expanded":      expanded_terms,          # usados apenas para ranking local
        "phrases":       phrases,
        "days_filter":   days_filter,
        "field_hints":   field_hints,
    }


def _build_search_jql(parsed: dict, project_keys: list, days: int = None) -> str:
    """Constroi JQL otimizado a partir dos termos parseados.

    Estrategia:
    - Frases exatas (entre aspas) → text ~ '"frase exata"'
    - Sem frase: 1 ancora principal (termo mais longo e especifico) com AND
      + 1 termo secundario opcional
    - Nunca mais de 2 termos com AND em text~ para evitar zero resultados
    - Campo especifico (description/comment) so se houver hint E o termo
      tem >= 6 chars (especifico o suficiente)
    - Expandidos sao usados apenas para ranking local, nao no JQL
    """
    proj = f'project in ({",".join(project_keys)})'

    phrases      = parsed["phrases"]
    all_terms    = parsed["terms"]   # ja sao os originais, sem expandidos
    hints        = parsed["field_hints"]
    days_filter  = days or parsed.get("days_filter")

    # Selecionar termos ancora: os 2 mais longos (mais especificos) dos originais
    anchor_terms = sorted(set(all_terms), key=len, reverse=True)[:2]

    clauses = []

    if phrases:
        # Frase exata: altissima precisao
        for phrase in phrases:
            clauses.append(f'text ~ "\\"{phrase}\\""')
    elif hints and anchor_terms:
        # Busca com campo especifico + fallback geral
        # Usa o campo sugerido com o termo principal
        field_parts = []
        for field in hints[:1]:  # apenas o primeiro hint para nao complicar
            field_parts.append(f'{field} ~ "{anchor_terms[0]}"')
        # Adiciona busca geral (text) para nao perder nada
        general_parts = [f'text ~ "{t}"' for t in anchor_terms[:2]]
        combined = " OR ".join(
            [("(" + " AND ".join(field_parts) + ")")] + [("(" + " AND ".join(general_parts) + ")")]
        )
        clauses.append(combined)
    else:
        # Busca geral com 1-2 termos em AND
        text_parts = [f'text ~ "{t}"' for t in anchor_terms[:2]]
        clauses.append(" AND ".join(text_parts))

    date_clause = ""
    if days_filter:
        since = (date.today() - timedelta(days=days_filter)).isoformat()
        date_clause = f' AND updated >= "{since}"'

    jql_body = " AND ".join(clauses) if clauses else f'text ~ "{all_terms[0]}"'
    return f"{proj} AND ({jql_body}){date_clause} ORDER BY updated DESC"


def search_content(query: str, project_keys=None, days: int = None,
                   max_results: int = 20) -> dict:
    """Pesquisa avancada em descricoes, criterios de aceite, cenarios e comentarios.

    Aceita linguagem natural em portugues ou ingles.
    Retorna resultados com trechos contextualizados, campo de origem e datas.

    Exemplos de queries:
      - "como foi combinada a regra de calculo do CCEE"
      - "criterio de aceite para fatura de venda"
      - "cenarios de teste do campo de migracao"
      - "o que foi decidido sobre garantia nos comentarios"
      - '"tag contrato" nos ultimos 30 dias'
    """
    projects = project_keys or PROJECTS
    parsed   = _parse_natural_query(query)
    terms    = parsed["terms"]
    # Para snippets e match local: usar todos os termos (originais + expandidos)
    all_terms_local = terms + parsed.get("expanded", [])

    if not terms:
        return {"erro": "Nao foi possivel extrair termos de busca da query informada."}

    jql = _build_search_jql(parsed, projects, days)

    # Buscar com conteudo completo — description + comments
    issues = _search(
        jql,
        "summary,status,assignee,issuetype,created,updated,priority,description,comment",
        max_results=max_results
    )

    results = []
    for issue in issues:
        f    = issue["fields"]
        key  = issue["key"]

        # Extrair texto da descricao (ADF)
        desc_text = _adf_to_text(f.get("description"))

        # Extrair comentarios com metadados
        raw_comments = (f.get("comment") or {}).get("comments", [])
        comments = [
            {
                "author": (c.get("author") or {}).get("displayName", "?"),
                "date":   c.get("created", "")[:16].replace("T", " "),
                "text":   _adf_to_text(c.get("body")),
            }
            for c in raw_comments
        ]
        total_comments = (f.get("comment") or {}).get("total", len(comments))

        # Encontrar matches por campo
        matches = []

        # 1. Descricao
        if desc_text and any(t.lower() in desc_text.lower() for t in all_terms_local):
            matches.append({
                "campo":  "descricao",
                "trecho": _extract_snippet(desc_text, all_terms_local),
            })

        # 2. Comentarios
        for c in comments:
            if any(t.lower() in c["text"].lower() for t in all_terms_local):
                matches.append({
                    "campo":  f'comentario — {c["author"]} ({c["date"]})',
                    "trecho": _extract_snippet(c["text"], all_terms_local),
                })

        # 3. Resumo (fallback se nao achou nos outros campos)
        if not matches:
            matches.append({
                "campo":  "resumo",
                "trecho": f.get("summary", ""),
            })

        results.append({
            "key":         key,
            "resumo":      f.get("summary", ""),
            "tipo":        (f.get("issuetype") or {}).get("name", ""),
            "status":      (f.get("status") or {}).get("name", ""),
            "responsavel": (f.get("assignee") or {}).get("displayName", "—"),
            "criado":      f.get("created", "")[:10],
            "atualizado":  f.get("updated", "")[:10],
            "total_comentarios": total_comments,
            "matches":     matches,
        })

    return {
        "query":         query,
        "termos_usados": terms,
        "expandidos":    parsed.get("expanded", []),
        "jql_gerado":    jql,
        "total":         len(results),
        "resultados":    results,
    }


# ---------------------------------------------------------------------------
# Modulo 6: PRs e Código — Development Info + Remote Links
# ---------------------------------------------------------------------------

def _get_issue_numeric_id(issue_key: str) -> str:
    """Retorna o ID numerico de um issue a partir da chave (ex: TSRV-1263 -> 80084)."""
    data = _get(f"/issue/{issue_key}", params={"fields": "id"})
    return data.get("id")


def get_issue_prs(issue_key: str) -> dict:
    """Retorna PRs, commits e branches vinculados a um issue via integração Jira-Bitbucket.

    Usa a Dev-Status API interna do Jira Cloud (applicationType=bitbucket).
    Inclui também fallback de busca por URLs de PR/commit nos textos do issue.

    Retorna:
      pull_requests: [{title, status, url, repo, author, created, source_branch, target_branch, merged}]
      commits: [{message, url, author, date, repo}]
      branches: [{name, url, repo}]
      remote_links: [{title, url, type}]  ← links manuais adicionados via "Link" no Jira
      text_links: [{url, source}]  ← URLs de PR/commit encontradas nos textos (fallback)
    """
    issue_id = _get_issue_numeric_id(issue_key)
    if not issue_id:
        return {"erro": f"Issue {issue_key} nao encontrado"}

    result = {
        "issue":         issue_key,
        "pull_requests": [],
        "commits":       [],
        "branches":      [],
        "remote_links":  [],
        "text_links":    [],
    }

    # 1. Dev-Status API — PRs via Bitbucket
    try:
        r = requests.get(
            f"{JIRA_DEVINFO}/issue/detail",
            auth=_auth(),
            headers={"Accept": "application/json"},
            params={"issueId": issue_id, "applicationType": "bitbucket", "dataType": "pullrequest"},
        )
        if r.ok:
            for detail in r.json().get("detail", []):
                for pr in detail.get("pullRequests", []):
                    result["pull_requests"].append({
                        "title":         pr.get("name", ""),
                        "status":        pr.get("status", ""),
                        "url":           pr.get("url", ""),
                        "repo":          pr.get("repositoryName", ""),
                        "author":        (pr.get("author") or {}).get("name", "—"),
                        "created":       (pr.get("lastUpdate") or "")[:16].replace("T", " "),
                        "source_branch": (pr.get("source") or {}).get("branch", "—"),
                        "target_branch": (pr.get("destination") or {}).get("branch", "—"),
                        "merged":        pr.get("status", "").upper() == "MERGED",
                        "approved":      any(
                            rev.get("approved") for rev in pr.get("reviewers", [])
                        ),
                    })
    except Exception:
        pass

    # 2. Dev-Status API — Commits e Branches via Bitbucket
    try:
        r = requests.get(
            f"{JIRA_DEVINFO}/issue/detail",
            auth=_auth(),
            headers={"Accept": "application/json"},
            params={"issueId": issue_id, "applicationType": "bitbucket", "dataType": "repository"},
        )
        if r.ok:
            for detail in r.json().get("detail", []):
                for commit in detail.get("commits", []):
                    result["commits"].append({
                        "message": commit.get("message", "")[:120],
                        "url":     commit.get("url", ""),
                        "author":  (commit.get("author") or {}).get("name", "—"),
                        "date":    (commit.get("authorTimestamp") or "")[:16].replace("T", " "),
                        "repo":    commit.get("repositoryName", ""),
                    })
                for branch in detail.get("branches", []):
                    result["branches"].append({
                        "name": branch.get("name", ""),
                        "url":  branch.get("url", ""),
                        "repo": branch.get("repositoryName", ""),
                    })
    except Exception:
        pass

    # 3. Remote Issue Links — links manuais colados via "Vincular" no Jira
    try:
        remote = _get(f"/issue/{issue_key}/remotelink")
        for link in (remote if isinstance(remote, list) else []):
            obj = link.get("object", {})
            url = obj.get("url", "")
            result["remote_links"].append({
                "title": obj.get("title", ""),
                "url":   url,
                "type":  link.get("relationship", ""),
                "is_pr": "pull" in url.lower() or "/pr/" in url.lower(),
            })
    except Exception:
        pass

    # 4. Campo GMUD (customfield_11536) — link de GMUD/wiki de deploy no Azure DevOps
    try:
        issue_data = _get(
            f"/issue/{issue_key}",
            params={"fields": "description,comment,customfield_11536,summary,status,assignee,issuetype"},
        )
        f = issue_data["fields"]

        gmud_val = f.get("customfield_11536")
        if gmud_val:
            gmud_text = _adf_to_text(gmud_val) if isinstance(gmud_val, dict) else str(gmud_val)
            # Extrai URLs do texto do GMUD
            for url in re.findall(r'https?://[^\s\)\]"\'<>]+', gmud_text):
                result["remote_links"].append({
                    "title": "GMUD",
                    "url":   url.strip(),
                    "type":  "gmud",
                    "is_pr": "pullrequest" in url.lower() or "/pr/" in url.lower(),
                })

        # 5. Fallback — varrer descrição e comentários buscando URLs de PR/commit
        # Suporta Azure DevOps, Bitbucket, GitHub e GitLab
        _pr_url_re = re.compile(
            r'https?://(?:'
            r'dev\.azure\.com/[^\s\)\]"\'<>]+(?:pullrequest|commit|_git)[^\s\)\]"\'<>]*'
            r'|bitbucket\.org/[^\s\)\]"\'<>]+(?:pull-requests?|commits?)[^\s\)\]"\'<>]*'
            r'|github\.com/[^\s\)\]"\'<>]+(?:pulls?|commit)[^\s\)\]"\'<>]*'
            r'|gitlab\.com/[^\s\)\]"\'<>]+(?:merge_requests?|commit)[^\s\)\]"\'<>]*'
            r')',
            re.I,
        )

        # Descrição
        desc_text = _adf_to_text(f.get("description"))
        for url in _pr_url_re.findall(desc_text):
            result["text_links"].append({"url": url, "source": "descricao"})

        # Comentários
        for c in (f.get("comment") or {}).get("comments", []):
            author = (c.get("author") or {}).get("displayName", "?")
            c_text = _adf_to_text(c.get("body"))
            for url in _pr_url_re.findall(c_text):
                result["text_links"].append({
                    "url":    url,
                    "source": f"comentario — {author}",
                })

        # Deduplicar text_links
        seen = set()
        deduped = []
        for item in result["text_links"]:
            if item["url"] not in seen:
                seen.add(item["url"])
                deduped.append(item)
        result["text_links"] = deduped

    except Exception:
        pass

    return result


def get_prs_for_stories(issue_keys: list) -> list:
    """Retorna PRs de uma lista de issues em batch.

    Util para verificar quais historias de um sprint tem PR associado.
    Retorna lista [{key, summary_prs: int, prs: [...]}]
    """
    results = []
    for key in issue_keys:
        try:
            pr_data = get_issue_prs(key)
            results.append({
                "key":        key,
                "total_prs":  len(pr_data.get("pull_requests", [])),
                "total_commits": len(pr_data.get("commits", [])),
                "prs":        pr_data.get("pull_requests", []),
                "branches":   pr_data.get("branches", []),
                "remote_links": [l for l in pr_data.get("remote_links", []) if l.get("is_pr")],
            })
        except Exception as e:
            results.append({"key": key, "erro": str(e)})
    return results


def find_story_by_pr_pattern(issue_key: str, pr_url_pattern: str = None) -> dict:
    """Dado um issue, retorna todos os artefatos de código relacionados formatados.

    Combina PRs, commits e branches em uma visão consolidada para rastreabilidade.
    Se pr_url_pattern fornecido, filtra PRs/remote_links que contenham o padrão.
    """
    raw = get_issue_prs(issue_key)

    # Informacoes basicas do issue (ja carregadas dentro de get_issue_prs se disponivel)
    try:
        issue_data = _get(
            f"/issue/{issue_key}",
            params={"fields": "summary,status,assignee,issuetype,customfield_11536"},
        )
        f = issue_data["fields"]
        gmud_val = f.get("customfield_11536")
        gmud_text = _adf_to_text(gmud_val) if isinstance(gmud_val, dict) else (str(gmud_val) if gmud_val else None)
        issue_info = {
            "key":         issue_key,
            "summary":     f.get("summary", ""),
            "status":      f["status"]["name"],
            "assignee":    (f.get("assignee") or {}).get("displayName", "—"),
            "tipo":        f["issuetype"]["name"],
            "gmud":        gmud_text,
        }
    except Exception:
        issue_info = {"key": issue_key}

    prs     = raw.get("pull_requests", [])
    commits = raw.get("commits", [])
    branches = raw.get("branches", [])
    remote  = [l for l in raw.get("remote_links", []) if l.get("is_pr")]

    if pr_url_pattern:
        prs    = [p for p in prs    if pr_url_pattern.lower() in p.get("url", "").lower()]
        remote = [l for l in remote if pr_url_pattern.lower() in l.get("url", "").lower()]

    # Status resumido
    merged   = [p for p in prs if p.get("merged")]
    open_prs = [p for p in prs if not p.get("merged")]

    all_remote = raw.get("remote_links", [])
    gmud_links = [l for l in all_remote if l.get("type") == "gmud"]

    return {
        "issue":          issue_info,
        "resumo": {
            "total_prs":      len(prs) + len(remote),
            "prs_merged":     len(merged),
            "prs_abertos":    len(open_prs),
            "total_commits":  len(commits),
            "total_branches": len(branches),
            "tem_gmud":       bool(issue_info.get("gmud")),
        },
        "gmud":           issue_info.get("gmud"),
        "pull_requests":  prs,
        "remote_pr_links": remote,
        "gmud_links":     gmud_links,
        "commits":        commits[:10],
        "branches":       branches,
        "text_links":     raw.get("text_links", []),
    }


# ---------------------------------------------------------------------------
# Modulo 9: Worklog Hours — Analise de Horas Apontadas
# ---------------------------------------------------------------------------

def _extract_worklog_text(comment_field) -> str:
    if not comment_field:
        return ""
    if isinstance(comment_field, str):
        return comment_field
    texts = []
    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
    _walk(comment_field)
    return " ".join(texts).strip()


def _fetch_all_worklog_ids(since_days: int) -> list:
    """Retorna todos os IDs de worklogs atualizados nos ultimos N dias via paginacao."""
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp() * 1000)
    ids = []
    since_cursor = since_ms
    while True:
        data = _get("/worklog/updated", params={"since": since_cursor})
        ids.extend(v["worklogId"] for v in data.get("values", []))
        if data.get("lastPage", True):
            break
        since_cursor = data.get("until", since_cursor + 1)
    return ids


def _fetch_worklog_details(ids: list) -> list:
    """Busca detalhes completos de ate 1000 worklogs por requisicao."""
    results = []
    for i in range(0, len(ids), 1000):
        batch = ids[i:i+1000]
        results.extend(_post("/worklog/list", {"ids": batch}))
    return results


def _resolve_issue_ids(issue_ids: list) -> dict:
    """Resolve issueId (numerico) -> {key, project, summary, status, issuetype}."""
    if not issue_ids:
        return {}
    mapping = {}
    # Usa JQL id in (...) para resolver em batch (max 200 por vez)
    unique_ids = list(set(str(i) for i in issue_ids))
    for i in range(0, len(unique_ids), 200):
        batch = unique_ids[i:i+200]
        jql = f"id in ({','.join(batch)})"
        issues = _search(jql, "summary,status,project,issuetype", max_results=200)
        for issue in issues:
            mapping[str(issue["id"])] = {
                "key":       issue["key"],
                "project":   issue["fields"]["project"]["key"],
                "summary":   issue["fields"].get("summary", ""),
                "status":    issue["fields"]["status"]["name"],
                "issuetype": issue["fields"]["issuetype"]["name"],
            }
    return mapping


def get_worklog_hours(
    since_days: int = 30,
    person: str = None,
    project_keys: list = None,
    keyword: str = None,
    fetch_window_multiplier: int = 3,
) -> dict:
    """
    Analisa horas apontadas via worklog no Jira.

    Parametros:
        since_days    : periodo em dias (padrao 30)
        person        : filtrar por nome do autor (parcial, case-insensitive)
        project_keys  : filtrar por lista de projetos (ex: ["TPROJ", "TSRV"])
        keyword       : filtrar por palavra no comentario do worklog (ex: "teste")

    Retorna dict com:
        total_horas, total_lancamentos,
        por_pessoa   [{nome, horas, lancamentos}],
        por_projeto  [{projeto, horas, lancamentos}],
        por_issue    [{key, projeto, summary, status, horas}],
        detalhes     [{author, date, horas, issue_key, projeto, summary, comment}],
        periodo_dias, filtros_aplicados
    """
    # 1. Buscar IDs — usa janela maior para compensar diferenca entre
    #    data de atualizacao do registro e data de inicio do trabalho (started)
    fetch_days = since_days * fetch_window_multiplier
    ids = _fetch_all_worklog_ids(fetch_days)
    if not ids:
        return {
            "total_horas": 0, "total_lancamentos": 0,
            "por_pessoa": [], "por_projeto": [], "por_issue": [], "detalhes": [],
            "periodo_dias": since_days, "filtros_aplicados": {},
            "aviso": "Nenhum worklog encontrado no periodo."
        }

    # 2. Buscar detalhes
    worklogs = _fetch_worklog_details(ids)

    # 2b. Filtrar pelo campo `started` (data real do trabalho) dentro do periodo pedido
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    worklogs = [w for w in worklogs if (w.get("started") or "")[:10] >= cutoff]

    # 3. Filtrar por pessoa — match por palavras individuais (ignora nomes do meio)
    if person:
        person_words = [p for p in person.lower().split() if len(p) > 1]
        worklogs = [w for w in worklogs
                    if all(pw in w.get("author", {}).get("displayName", "").lower()
                           for pw in person_words)]

    # 4. Filtrar por keyword no comentario
    if keyword:
        kw_lower = keyword.lower()
        worklogs = [w for w in worklogs
                    if kw_lower in _extract_worklog_text(w.get("comment", "")).lower()]

    # 5. Resolver issueIds -> keys/projeto
    issue_ids = list({str(w["issueId"]) for w in worklogs})
    issue_map = _resolve_issue_ids(issue_ids)

    # 6. Filtrar por projeto
    if project_keys:
        proj_upper = [p.upper() for p in project_keys]
        worklogs = [w for w in worklogs
                    if issue_map.get(str(w["issueId"]), {}).get("project", "") in proj_upper]

    # 7. Agregar
    from collections import defaultdict
    total_horas = 0.0
    por_pessoa   = defaultdict(lambda: {"horas": 0.0, "lancamentos": 0})
    por_projeto  = defaultdict(lambda: {"horas": 0.0, "lancamentos": 0})
    por_issue    = defaultdict(lambda: {"horas": 0.0, "lancamentos": 0, "summary": "", "projeto": "", "status": ""})
    detalhes     = []

    for w in worklogs:
        autor     = w.get("author", {}).get("displayName", "Desconhecido")
        horas     = round(w.get("timeSpentSeconds", 0) / 3600, 2)
        date      = (w.get("started") or "")[:10]
        comment   = _extract_worklog_text(w.get("comment", ""))
        info      = issue_map.get(str(w["issueId"]), {})
        issue_key = info.get("key", f'ID:{w["issueId"]}')
        projeto   = info.get("project", "?")
        summary   = info.get("summary", "")
        status    = info.get("status", "?")

        total_horas += horas
        por_pessoa[autor]["horas"] += horas
        por_pessoa[autor]["lancamentos"] += 1
        por_projeto[projeto]["horas"] += horas
        por_projeto[projeto]["lancamentos"] += 1
        por_issue[issue_key]["horas"] += horas
        por_issue[issue_key]["lancamentos"] += 1
        por_issue[issue_key]["summary"]  = summary
        por_issue[issue_key]["projeto"]  = projeto
        por_issue[issue_key]["status"]   = status
        detalhes.append({
            "author":    autor,
            "date":      date,
            "horas":     horas,
            "issue_key": issue_key,
            "projeto":   projeto,
            "summary":   summary[:80],
            "status":    status,
            "comment":   comment[:120],
        })

    # Ordenar
    por_pessoa_list  = sorted(
        [{"nome": k, **v} for k, v in por_pessoa.items()],
        key=lambda x: -x["horas"]
    )
    por_projeto_list = sorted(
        [{"projeto": k, **v} for k, v in por_projeto.items()],
        key=lambda x: -x["horas"]
    )
    por_issue_list   = sorted(
        [{"key": k, **v} for k, v in por_issue.items()],
        key=lambda x: -x["horas"]
    )
    detalhes_sorted  = sorted(detalhes, key=lambda x: x["date"])

    return {
        "total_horas":      round(total_horas, 1),
        "total_lancamentos": len(worklogs),
        "por_pessoa":        por_pessoa_list,
        "por_projeto":       por_projeto_list,
        "por_issue":         por_issue_list[:20],
        "detalhes":          detalhes_sorted,
        "periodo_dias":      since_days,
        "filtros_aplicados": {
            "person":       person,
            "project_keys": project_keys,
            "keyword":      keyword,
        },
    }


_WORKLOG_STATUS_TESTE = {
    "em teste", "in test", "em homologacao", "homologacao", "homologação",
    "em homologação", "qa", "in review", "em revisao", "em revisão",
    "validacao", "validação", "em validacao", "em validação",
    "aguardando homologacao", "aguardando validacao",
}
_WORKLOG_SUMMARY_TESTE = [
    "teste", "test", "testes", "testing", "qa", "homolog",
    "validac", "regressao", "regressão", "evidenci",
]


def _is_test_worklog(status: str, summary: str, comment: str) -> tuple:
    s  = (status  or "").lower()
    sm = (summary or "").lower()
    c  = (comment or "").lower()
    if s in _WORKLOG_STATUS_TESTE:
        return True, f"status:{status}"
    for kw in _WORKLOG_SUMMARY_TESTE:
        if kw in sm:
            return True, f"summary:'{kw}'"
        if kw in c:
            return True, f"comment:'{kw}'"
    return False, None


def get_team_members(group_name: str) -> list:
    """
    Retorna membros de um grupo Jira.
    Retorna lista de dicts: {accountId, displayName, emailAddress}
    Retorna lista vazia se o grupo nao existir ou sem permissao.
    """
    try:
        data = _get("/group/member", params={"groupname": group_name, "maxResults": 100})
        return data.get("values", [])
    except Exception:
        pass
    # Fallback: busca por query parcial
    try:
        data = _get("/groups/picker", params={"query": group_name, "maxResults": 10})
        for g in data.get("groups", []):
            if group_name.lower() in g["name"].lower():
                members = _get("/group/member",
                               params={"groupname": g["name"], "maxResults": 100})
                return members.get("values", [])
    except Exception:
        pass
    return []


def analyze_worklog_hours(
    since_days: int = 21,
    person: str = None,
    project_keys: list = None,
    group_name: str = None,
    classify_test: bool = True,
) -> dict:
    """
    Analise completa de horas apontadas com classificacao de atividade de teste.

    Parametros:
        since_days    : periodo em dias (padrao 21)
        person        : filtrar por nome do autor (parcial, case-insensitive)
        project_keys  : filtrar por lista de projetos (ex: ["TPROJ"])
        group_name    : nome do grupo Jira para filtrar por time (ex: "[DEV] Projetos")
        classify_test : se True, classifica cada worklog como teste ou nao-teste

    Retorna dict com:
        total_horas, total_teste, total_outras,
        por_pessoa   [{nome, total, teste, pct_teste, lancamentos, lancamentos_teste}],
        por_projeto  [{projeto, total, teste}],
        por_issue_teste [{key, horas, summary, status}],
        detalhes_teste  [{date, autor, horas, key, summary, motivo}],
        periodo_dias, grupo_encontrado, membros_grupo,
        aviso (quando grupo nao encontrado ou dados parciais)
    """
    # Resolver membros do grupo se informado
    membros_grupo = []
    grupo_encontrado = False
    aviso = None

    if group_name:
        membros_grupo = get_team_members(group_name)
        if membros_grupo:
            grupo_encontrado = True
        else:
            aviso = (f"Grupo '{group_name}' nao encontrado via API de grupos. "
                     f"Usando filtro por projeto {project_keys or 'TPROJ'} como alternativa.")

    # Buscar worklogs base
    result = get_worklog_hours(
        since_days=since_days,
        person=person,
        project_keys=project_keys,
    )

    if result["total_lancamentos"] == 0:
        return {
            "total_horas": 0, "total_teste": 0, "total_outras": 0,
            "por_pessoa": [], "por_projeto": [], "por_issue_teste": [],
            "detalhes_teste": [], "periodo_dias": since_days,
            "grupo_encontrado": grupo_encontrado,
            "membros_grupo": [m["displayName"] for m in membros_grupo],
            "aviso": aviso or "Nenhum worklog encontrado no periodo.",
        }

    # Filtrar por membros do grupo se resolvido
    detalhes = result["detalhes"]
    if grupo_encontrado and membros_grupo:
        ids_grupo = {m["accountId"] for m in membros_grupo}
        nomes_grupo = {m["displayName"].lower() for m in membros_grupo}
        detalhes = [d for d in detalhes
                    if d["author"].lower() in nomes_grupo]

    # Classificar teste vs nao-teste
    from collections import defaultdict
    por_pessoa   = defaultdict(lambda: {"total": 0.0, "teste": 0.0,
                                        "lancamentos": 0, "lancamentos_teste": 0})
    por_projeto  = defaultdict(lambda: {"total": 0.0, "teste": 0.0})
    por_issue_t  = defaultdict(lambda: {"horas": 0.0, "summary": "", "status": ""})
    det_teste    = []

    for d in detalhes:
        autor   = d["author"]
        horas   = d["horas"]
        projeto = d["projeto"]
        key     = d["issue_key"]
        status  = d["status"]
        summary = d["summary"]
        comment = d.get("comment", "")

        por_pessoa[autor]["total"]       += horas
        por_pessoa[autor]["lancamentos"] += 1
        por_projeto[projeto]["total"]    += horas

        if classify_test:
            eh_teste, motivo = _is_test_worklog(status, summary, comment)
            if eh_teste:
                por_pessoa[autor]["teste"]            += horas
                por_pessoa[autor]["lancamentos_teste"] += 1
                por_projeto[projeto]["teste"]          += horas
                por_issue_t[key]["horas"]             += horas
                por_issue_t[key]["summary"]            = summary
                por_issue_t[key]["status"]             = status
                det_teste.append({
                    "date": d["date"], "autor": autor, "horas": horas,
                    "key": key, "summary": summary[:60], "motivo": motivo,
                })

    total_horas = sum(v["total"] for v in por_pessoa.values())
    total_teste = sum(v["teste"] for v in por_pessoa.values())

    por_pessoa_list = sorted(
        [{"nome": k,
          "total": round(v["total"], 1),
          "teste": round(v["teste"], 1),
          "pct_teste": round(v["teste"] / v["total"] * 100, 1) if v["total"] else 0,
          "lancamentos": v["lancamentos"],
          "lancamentos_teste": v["lancamentos_teste"]}
         for k, v in por_pessoa.items()],
        key=lambda x: -x["total"]
    )
    por_projeto_list = sorted(
        [{"projeto": k, "total": round(v["total"], 1), "teste": round(v["teste"], 1)}
         for k, v in por_projeto.items()],
        key=lambda x: -x["total"]
    )
    por_issue_list = sorted(
        [{"key": k, "horas": round(v["horas"], 1),
          "summary": v["summary"], "status": v["status"]}
         for k, v in por_issue_t.items()],
        key=lambda x: -x["horas"]
    )

    return {
        "total_horas":      round(total_horas, 1),
        "total_teste":      round(total_teste, 1),
        "total_outras":     round(total_horas - total_teste, 1),
        "pct_teste":        round(total_teste / total_horas * 100, 1) if total_horas else 0,
        "por_pessoa":       por_pessoa_list,
        "por_projeto":      por_projeto_list,
        "por_issue_teste":  por_issue_list,
        "detalhes_teste":   sorted(det_teste, key=lambda x: x["date"]),
        "periodo_dias":     since_days,
        "grupo_encontrado": grupo_encontrado,
        "membros_grupo":    [m["displayName"] for m in membros_grupo],
        "aviso":            aviso,
    }


# ---------------------------------------------------------------------------
# Modulo 7: Azure DevOps — PRs, Commits e Analise N1
# ---------------------------------------------------------------------------

import base64 as _base64

AZURE_DEVOPS_BASE  = "https://dev.azure.com"
AZURE_SEARCH_BASE  = "https://almsearch.dev.azure.com"

# Cache de repositorios por projeto (evita chamadas repetidas)
_az_repos_cache: dict = {}


def _az_configured() -> bool:
    """Retorna True se as credenciais do Azure DevOps estao configuradas."""
    return bool(
        os.environ.get("AZURE_DEVOPS_ORG") and
        os.environ.get("AZURE_DEVOPS_PAT")
    )


def _az_headers() -> dict:
    pat = os.environ.get("AZURE_DEVOPS_PAT", "")
    b64 = _base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {b64}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _az_get(url: str, params: dict = None) -> dict:
    r = requests.get(url, headers=_az_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _az_post(url: str, body: dict, params: dict = None) -> dict:
    r = requests.post(url, headers=_az_headers(), json=body, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _get_azure_repos(org: str, project: str) -> list:
    """Lista repositorios de um projeto Azure DevOps (resultado em cache por sessao)."""
    cache_key = f"{org}/{project}"
    if cache_key in _az_repos_cache:
        return _az_repos_cache[cache_key]
    url = f"{AZURE_DEVOPS_BASE}/{org}/{project}/_apis/git/repositories"
    data = _az_get(url, {"api-version": "7.0"})
    repos = data.get("value", [])
    _az_repos_cache[cache_key] = repos
    return repos


def _az_pr_to_dict(pr: dict, org: str, project: str, repo_name: str) -> dict:
    """Normaliza um objeto PR do Azure DevOps para formato padrao."""
    pr_id = pr.get("pullRequestId")
    return {
        "pr_id":         pr_id,
        "title":         pr.get("title", ""),
        "status":        pr.get("status", ""),  # active | completed | abandoned
        "url":           f"https://dev.azure.com/{org}/{project}/_git/{repo_name}/pullrequest/{pr_id}",
        "repo":          repo_name,
        "author":        pr.get("createdBy", {}).get("displayName", "—"),
        "created":       (pr.get("creationDate") or "")[:16].replace("T", " "),
        "completed":     (pr.get("closedDate") or pr.get("completionQueueTime") or "")[:16].replace("T", " ") or None,
        "source_branch": pr.get("sourceRefName", "").replace("refs/heads/", ""),
        "target_branch": pr.get("targetRefName", "").replace("refs/heads/", ""),
        "merged":        pr.get("status") == "completed",
        "description":   (pr.get("description") or "")[:300],
    }


def search_azure_prs(jira_key: str, az_project: str = None) -> list:
    """Busca PRs no Azure DevOps relacionados a uma chave Jira.

    Estrategia (em ordem):
    1. Azure DevOps PR Search API — busca rapida cross-repo por texto livre
    2. Fallback: lista repositorios + filtra PRs onde titulo, branch ou descricao
       contem a chave Jira (client-side)

    Requer env vars: AZURE_DEVOPS_ORG, AZURE_DEVOPS_PAT.
    AZURE_DEVOPS_PROJECT define o projeto padrao (pode ser sobrescrito por az_project).

    Retorna lista de dicts com metadados do PR.
    """
    if not _az_configured():
        return []

    org     = os.environ.get("AZURE_DEVOPS_ORG", "")
    project = az_project or os.environ.get("AZURE_DEVOPS_PROJECT", "")
    key_up  = jira_key.upper()
    prs_found: list = []

    # Estrategia 1: Azure Search API (requer extensao Code Search habilitada)
    try:
        url  = f"{AZURE_SEARCH_BASE}/{org}/_apis/search/pullrequestsearchresults"
        body = {
            "searchText":    jira_key,
            "$top":          25,
            "$skip":         0,
            "filters":       {"Project": [project]} if project else {},
            "includeFacets": False,
        }
        data = _az_post(url, body, params={"api-version": "7.0-preview.1"})
        for item in data.get("results", []):
            repo_name = item.get("repository", {}).get("name", "")
            pr_id     = item.get("pullRequestId")
            prs_found.append({
                "pr_id":         pr_id,
                "title":         item.get("title", ""),
                "status":        item.get("status", ""),
                "url":           f"https://dev.azure.com/{org}/{project}/_git/{repo_name}/pullrequest/{pr_id}",
                "repo":          repo_name,
                "author":        item.get("createdBy", {}).get("displayName", "—"),
                "created":       (item.get("creationDate") or "")[:16].replace("T", " "),
                "completed":     (item.get("closedDate") or "")[:16].replace("T", " ") or None,
                "source_branch": item.get("sourceRefName", "").replace("refs/heads/", ""),
                "target_branch": item.get("targetRefName", "").replace("refs/heads/", ""),
                "merged":        item.get("status") == "completed",
                "description":   (item.get("description") or "")[:300],
                "via":           "search_api",
            })
    except Exception:
        pass

    # Estrategia 2: listar repos e filtrar PRs client-side
    if not prs_found and project:
        try:
            repos = _get_azure_repos(org, project)
            since = (date.today() - timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z")

            for repo in repos:
                repo_id   = repo["id"]
                repo_name = repo["name"]
                url = (f"{AZURE_DEVOPS_BASE}/{org}/{project}"
                       f"/_apis/git/repositories/{repo_id}/pullrequests")
                params = {
                    "searchCriteria.status": "all",
                    "searchCriteria.minTime": since,
                    "$top": 200,
                    "api-version": "7.0",
                }
                data = _az_get(url, params)
                for pr in data.get("value", []):
                    title  = pr.get("title", "")
                    desc   = pr.get("description", "") or ""
                    source = pr.get("sourceRefName", "")
                    if (key_up in title.upper()
                            or key_up in desc.upper()
                            or key_up in source.upper()):
                        d = _az_pr_to_dict(pr, org, project, repo_name)
                        d["via"] = "repo_scan"
                        prs_found.append(d)
        except Exception:
            pass

    return prs_found


def search_azure_commits(jira_key: str, az_project: str = None,
                         days: int = 90) -> list:
    """Busca commits no Azure DevOps cujo commit message mencione a chave Jira.

    Pesquisa nos ultimos `days` dias em todos os repositorios do projeto.
    Usa searchCriteria.comment (filtro server-side — eficiente).

    Requer env vars: AZURE_DEVOPS_ORG, AZURE_DEVOPS_PAT, AZURE_DEVOPS_PROJECT.
    """
    if not _az_configured():
        return []

    org     = os.environ.get("AZURE_DEVOPS_ORG", "")
    project = az_project or os.environ.get("AZURE_DEVOPS_PROJECT", "")
    if not project:
        return []

    from_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    commits_found: list = []

    try:
        repos = _get_azure_repos(org, project)
        for repo in repos:
            repo_id   = repo["id"]
            repo_name = repo["name"]
            url = (f"{AZURE_DEVOPS_BASE}/{org}/{project}"
                   f"/_apis/git/repositories/{repo_id}/commits")
            params = {
                "searchCriteria.fromDate":  from_date,
                "searchCriteria.comment":   jira_key,
                "$top": 20,
                "api-version": "7.0",
            }
            data = _az_get(url, params)
            for c in data.get("value", []):
                commit_id = c.get("commitId", "")
                commits_found.append({
                    "commit_id":   commit_id[:8],
                    "commit_full": commit_id,
                    "message":     (c.get("comment") or "")[:200],
                    "author":      c.get("author", {}).get("name", "—"),
                    "date":        (c.get("author", {}).get("date") or "")[:16].replace("T", " "),
                    "repo":        repo_name,
                    "url":         (c.get("remoteUrl")
                                   or f"https://dev.azure.com/{org}/{project}"
                                      f"/_git/{repo_name}/commit/{commit_id}"),
                })
    except Exception:
        pass

    return commits_found


def get_azure_pr_files(org: str, project: str, repo: str, pr_id: int) -> list:
    """Retorna lista de arquivos alterados em um PR do Azure DevOps.

    `repo` pode ser o nome ou ID do repositorio.
    Busca a ultima iteracao do PR (estado final das mudancas).
    """
    try:
        iter_url = (f"{AZURE_DEVOPS_BASE}/{org}/{project}"
                    f"/_apis/git/repositories/{repo}/pullRequests/{pr_id}/iterations")
        iterations = _az_get(iter_url, {"api-version": "7.0"}).get("value", [])
        if not iterations:
            return []
        last_iter = iterations[-1]["id"]

        changes_url = (f"{AZURE_DEVOPS_BASE}/{org}/{project}"
                       f"/_apis/git/repositories/{repo}/pullRequests/{pr_id}"
                       f"/iterations/{last_iter}/changes")
        changes = _az_get(changes_url, {"api-version": "7.0"})
        return [
            {
                "file":        c.get("item", {}).get("path", ""),
                "change_type": c.get("changeType", ""),
            }
            for c in changes.get("changeEntries", [])
        ]
    except Exception:
        return []


def analyze_ticket_n1(issue_key: str, include_pr_files: bool = False) -> dict:
    """Analise completa N1 de um ticket Jira.

    Combina:
    - Dados completos do issue (tipo, status, descricao, criterios de aceite, comentarios)
    - Issue pai (historia/epico para contexto hierarquico)
    - PRs no Azure DevOps que implementaram a funcionalidade (busca por chave Jira)
    - Commits no Azure DevOps com a chave no commit message
    - PRs via Jira Dev-Status API (Bitbucket) e links remotos
    - Link GMUD (documentacao de deploy/mudanca no Azure DevOps Wiki)

    Parametros:
      issue_key        — chave Jira (ex: TSRV-1263, TLIGHTCOM-987)
      include_pr_files — se True, busca arquivos alterados nos PRs do Azure (chamada extra por PR)

    Retorna dict estruturado pronto para apresentacao N1.
    """
    from pathlib import Path
    import json

    # Campos customizados
    fields_cache = Path.home() / ".claude" / "jira-epic-automator-fields.json"
    custom = {}
    try:
        if fields_cache.exists():
            with open(fields_cache) as fp:
                custom = json.load(fp)
    except Exception:
        pass
    gmud_fid = "customfield_11536"

    # --- 1. Dados do issue Jira ---
    issue_data = _get(
        f"/issue/{issue_key}",
        params={
            "expand": "changelog",
            "fields": (
                f"summary,status,assignee,issuetype,description,comment,"
                f"priority,created,updated,parent,subtasks,"
                f"{gmud_fid},customfield_10016,fixVersions"
            ),
        },
    )
    f = issue_data["fields"]

    issue_type    = f.get("issuetype", {}).get("name", "")
    current_status = f["status"]["name"]
    assignee      = (f.get("assignee") or {}).get("displayName", "—")

    # Hierarquia: pai e epico
    parent = f.get("parent") or {}
    parent_key     = parent.get("key", "")
    parent_summary = (parent.get("fields") or {}).get("summary", "")
    parent_status  = (parent.get("fields") or {}).get("status", {}).get("name", "")

    # Descricao completa (ADF → texto)
    desc_text = _adf_to_text(f.get("description"))

    # Comentarios — ultimos 5, mais recentes primeiro
    raw_comments = sorted(
        (f.get("comment") or {}).get("comments", []),
        key=lambda c: c.get("created", ""),
        reverse=True,
    )[:5]
    comments = [
        {
            "author": (c.get("author") or {}).get("displayName", "?"),
            "date":   c.get("created", "")[:16].replace("T", " "),
            "text":   _adf_to_text(c.get("body"))[:600],
        }
        for c in raw_comments
    ]

    # Fix versions (releases)
    fix_versions = [v.get("name", "") for v in (f.get("fixVersions") or [])]

    # GMUD
    gmud_val  = f.get(gmud_fid)
    gmud_text = (
        _adf_to_text(gmud_val) if isinstance(gmud_val, dict)
        else (str(gmud_val) if gmud_val else None)
    )
    # Extrair URLs do GMUD (links para Azure DevOps wiki)
    gmud_urls = []
    if gmud_text:
        gmud_urls = re.findall(r'https?://[^\s\)\]"\'<>]+', gmud_text)

    # --- 2. PRs no Azure DevOps ---
    org         = os.environ.get("AZURE_DEVOPS_ORG", "")
    az_project  = os.environ.get("AZURE_DEVOPS_PROJECT", "")
    azure_prs   = []
    azure_commits = []
    az_available = _az_configured()

    if az_available:
        azure_prs     = search_azure_prs(issue_key)
        azure_commits = search_azure_commits(issue_key)

        # Se nao achou PRs diretos, tenta pelo pai (epico/historia)
        if not azure_prs and parent_key:
            azure_prs = search_azure_prs(parent_key)
            for pr in azure_prs:
                pr["via_parent"] = parent_key

        # Arquivos alterados (chamada extra por PR, opcional)
        if include_pr_files and azure_prs:
            for pr in azure_prs:
                repo  = pr.get("repo", "")
                pr_id = pr.get("pr_id")
                if repo and pr_id:
                    pr["files_changed"] = get_azure_pr_files(org, az_project, repo, pr_id)

    # --- 3. PRs via Jira (Bitbucket + remote links + texto) ---
    jira_code = get_issue_prs(issue_key)

    # --- 4. Compilar relatorio N1 ---
    # Determinar se a funcionalidade foi implementada (tem PR merged)
    merged_azure = [p for p in azure_prs if p.get("merged")]
    merged_jira  = [p for p in jira_code.get("pull_requests", []) if p.get("merged")]
    has_merged_pr = bool(merged_azure or merged_jira)

    return {
        "issue": {
            "key":            issue_key,
            "type":           issue_type,
            "summary":        f.get("summary", ""),
            "status":         current_status,
            "assignee":       assignee,
            "priority":       (f.get("priority") or {}).get("name", "—"),
            "created":        f.get("created", "")[:10],
            "updated":        f.get("updated", "")[:10],
            "fix_versions":   fix_versions,
        },
        "hierarquia": {
            "parent_key":     parent_key,
            "parent_summary": parent_summary,
            "parent_status":  parent_status,
        } if parent_key else None,
        "descricao":          desc_text[:2000] if desc_text else None,
        "ultimos_comentarios": comments,
        "azure": {
            "disponivel":     az_available,
            "org":            org,
            "project":        az_project,
            "prs":            azure_prs,
            "commits":        azure_commits,
            "pr_merged_count": len(merged_azure),
        },
        "jira_code": {
            "pull_requests":  jira_code.get("pull_requests", []),
            "remote_links":   jira_code.get("remote_links", []),
            "text_links":     jira_code.get("text_links", []),
            "commits":        jira_code.get("commits", []),
        },
        "gmud": {
            "texto":          gmud_text,
            "urls":           gmud_urls,
        },
        "implementacao": {
            "tem_pr_merged":  has_merged_pr,
            "repos_com_pr":   list({p["repo"] for p in azure_prs}),
            "branches":       list({p["source_branch"] for p in azure_prs if p.get("source_branch")}),
            "merged_em":      next(
                (p["completed"] for p in merged_azure if p.get("completed")), None
            ),
        },
    }


# ---------------------------------------------------------------------------
# Modulo 8: Analise Estrategica — Miro + Jira
# ---------------------------------------------------------------------------

MIRO_API_BASE = "https://api.miro.com/v2"


def _miro_configured() -> bool:
    return bool(os.environ.get("MIRO_API_TOKEN"))


def _miro_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('MIRO_API_TOKEN', '')}",
        "Accept": "application/json",
    }


def _extract_board_id(url_or_id: str) -> str:
    """Extrai o board ID de uma URL do Miro ou retorna o ID direto."""
    m = re.search(r"miro\.com/app/board/([^/?]+)", url_or_id)
    return m.group(1) if m else url_or_id


def _miro_get_all_items(board_id: str) -> list:
    """Busca todos os items de um board Miro com paginacao completa."""
    items = []
    cursor = None
    while True:
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(
            f"{MIRO_API_BASE}/boards/{board_id}/items",
            headers=_miro_headers(),
            params=params,
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"Miro API erro {r.status_code}: {r.text[:200]}")
        data = r.json()
        items.extend(data.get("data", []))
        cursor = data.get("cursor")
        if not cursor or not data.get("data"):
            break
    return items


def _miro_item_to_text(item: dict) -> str:
    """Extrai texto legivel de um item Miro (card, sticky, text, frame, shape)."""
    itype = item.get("type", "")
    data  = item.get("data", {})

    if itype == "card":
        title = data.get("title", "")
        desc  = data.get("description", "")
        return f"[CARD] {title}" + (f"\n  {desc}" if desc else "")

    if itype in ("sticky_note", "text"):
        content = data.get("content", "")
        # Remove tags HTML simples
        content = re.sub(r"<[^>]+>", " ", content).strip()
        return f"[{itype.upper()}] {content}" if content else ""

    if itype == "frame":
        title = data.get("title", "")
        return f"\n=== FRAME: {title} ===" if title else ""

    if itype == "shape":
        content = data.get("content", "")
        content = re.sub(r"<[^>]+>", " ", content).strip()
        return f"[SHAPE] {content}" if content else ""

    return ""


def read_miro_board(url_or_id: str) -> dict:
    """Le o conteudo completo de um board Miro.

    Requer env var: MIRO_API_TOKEN

    Retorna:
      board_id    — ID do board
      total_items — total de itens encontrados
      frames      — lista de frames com seus nomes (agrupadores visuais)
      cards       — cards (geralmente tarefas/iniciativas)
      sticky_notes — post-its (geralmente ideias, riscos, comentarios)
      texts       — textos soltos
      raw_text    — texto completo concatenado (para analise livre)

    Como obter o MIRO_API_TOKEN:
      1. Acesse https://miro.com/app/settings/user-profile/apps
      2. Clique em 'Create new app'  -> nome: 'Claude Analytics'
      3. Em 'OAuth & Permissions' -> Scopes -> marque 'boards:read'
      4. Clique 'Install app and get OAuth token'
      5. Copie o Access Token gerado
    """
    if not _miro_configured():
        return {
            "erro": (
                "MIRO_API_TOKEN nao configurado. "
                "Veja instrucoes em read_miro_board.__doc__ ou no SKILL.md."
            ),
            "disponivel": False,
        }

    board_id = _extract_board_id(url_or_id)
    items    = _miro_get_all_items(board_id)

    frames       = []
    cards        = []
    sticky_notes = []
    texts        = []
    text_lines   = []

    for item in items:
        itype = item.get("type", "")
        txt   = _miro_item_to_text(item)
        if txt:
            text_lines.append(txt)

        if itype == "frame":
            frames.append(item.get("data", {}).get("title", ""))
        elif itype == "card":
            d = item.get("data", {})
            cards.append({
                "title":       d.get("title", ""),
                "description": re.sub(r"<[^>]+>", " ", d.get("description", "") or "").strip(),
                "tags":        [t.get("title", "") for t in (d.get("tags") or [])],
            })
        elif itype == "sticky_note":
            content = re.sub(r"<[^>]+>", " ", item.get("data", {}).get("content", "") or "").strip()
            if content:
                sticky_notes.append(content)
        elif itype == "text":
            content = re.sub(r"<[^>]+>", " ", item.get("data", {}).get("content", "") or "").strip()
            if content:
                texts.append(content)

    return {
        "board_id":    board_id,
        "disponivel":  True,
        "total_items": len(items),
        "frames":      [f for f in frames if f],
        "cards":       cards,
        "sticky_notes": sticky_notes,
        "texts":       texts,
        "raw_text":    "\n".join(text_lines),
    }


def analyze_strategic_backlog(
    miro_url_or_id: str = None,
    jira_project: str = "MP",
    since_date: str = "2024-01-01",
    jira_board_id: int = None,
    miro_context: str = None,
    max_tickets: int = 100,
) -> dict:
    """Analise estrategica cruzando planejamento Miro com backlog Jira.

    Replica o fluxo do prompt:
      1. Le o board Miro (estrategia, epicos, temas)
      2. Busca tickets Jira do projeto/board a partir de since_date
      3. Cruza os dois para gerar relatorio de priorizacao

    Parametros:
      miro_url_or_id — URL ou ID do board Miro (ex: 'uXjVGrIugvU=')
      jira_project   — chave do projeto Jira (ex: 'MP', 'TPROJ')
      since_date     — data de corte no formato YYYY-MM-DD (ex: '2024-01-01')
      jira_board_id  — board ID Jira se quiser filtrar por board especifico
      miro_context   — texto manual com o planejamento estrategico (fallback sem token)
      max_tickets    — maximo de tickets Jira a buscar (padrao 100)

    Retorna dict com:
      miro        — dados do board (ou indicador de fallback)
      jira        — tickets encontrados com metadados
      relatorio   — lista de itens priorizados: descricao, categoria, clientes,
                    prioridade, solucao_proposta
      sem_cruzamento — tickets sem correspondencia estrategica clara
    """
    # --- 1. Conteudo estrategico do Miro ---
    miro_data = {}
    estrategia_texto = ""

    if miro_context:
        # Modo fallback: usuario colou o conteudo do Miro manualmente
        estrategia_texto = miro_context
        miro_data = {"disponivel": True, "modo": "manual", "raw_text": miro_context}
    elif miro_url_or_id:
        miro_data = read_miro_board(miro_url_or_id)
        if miro_data.get("disponivel"):
            estrategia_texto = miro_data.get("raw_text", "")
        else:
            estrategia_texto = ""
    else:
        miro_data = {"disponivel": False, "modo": "sem_miro"}

    # --- 2. Tickets Jira ---
    fields_jira = "summary,status,assignee,priority,issuetype,created,updated,description,comment,labels,components"

    if jira_board_id:
        # Busca por board especifico (sprint agile)
        sprint_data = _get(
            f"/board/{jira_board_id}/issue",
            params={
                "jql": f'created >= "{since_date}" ORDER BY priority ASC, created ASC',
                "maxResults": max_tickets,
                "fields": fields_jira,
            },
            base=JIRA_AGILE,
        )
        raw_issues = sprint_data.get("issues", [])
    else:
        # Busca por projeto
        jql = (
            f'project = {jira_project} '
            f'AND created >= "{since_date}" '
            f'AND issuetype not in subTaskIssueTypes() '
            f'ORDER BY priority ASC, created ASC'
        )
        raw_issues = _search(jql, fields_jira, max_results=max_tickets)

    # --- 3. Normalizar tickets ---
    tickets = []
    for issue in raw_issues:
        f = issue["fields"]
        desc = _adf_to_text(f.get("description"))
        comments = [
            _adf_to_text(c.get("body"))
            for c in (f.get("comment") or {}).get("comments", [])[-3:]
        ]
        tickets.append({
            "key":        issue["key"],
            "summary":    f.get("summary", ""),
            "status":     f["status"]["name"],
            "priority":   (f.get("priority") or {}).get("name", "—"),
            "type":       (f.get("issuetype") or {}).get("name", ""),
            "assignee":   (f.get("assignee") or {}).get("displayName", "—"),
            "created":    f.get("created", "")[:10],
            "labels":     f.get("labels", []),
            "components": [c.get("name", "") for c in (f.get("components") or [])],
            "description": desc[:500] if desc else "",
            "comments":   [c[:300] for c in comments if c],
        })

    # --- 4. Cruzamento estrategico ---
    # Extrai temas/frames do Miro como categorias
    temas = miro_data.get("frames", []) or []
    cards_miro = miro_data.get("cards", []) if miro_data.get("disponivel") else []

    relatorio = []
    sem_cruzamento = []

    for ticket in tickets:
        # Identifica categoria com base em labels, componentes ou correspondencia com frames Miro
        categoria = "—"
        if ticket["components"]:
            categoria = ticket["components"][0]
        elif ticket["labels"]:
            categoria = ticket["labels"][0]
        elif temas:
            # Tenta encontrar o frame do Miro mais relacionado ao ticket
            summary_lower = ticket["summary"].lower()
            for tema in temas:
                if any(w in summary_lower for w in tema.lower().split()):
                    categoria = tema
                    break

        # Identifica clientes (via labels, componentes ou keywords)
        clientes = ", ".join(ticket["components"]) if ticket["components"] else (
            ", ".join(ticket["labels"]) if ticket["labels"] else "—"
        )

        # Monta solucao proposta com base na descricao
        solucao = ticket["description"][:200] if ticket["description"] else "—"

        entrada = {
            "key":             ticket["key"],
            "descricao":       ticket["summary"],
            "categoria":       categoria,
            "clientes":        clientes,
            "prioridade":      ticket["priority"],
            "status":          ticket["status"],
            "assignee":        ticket["assignee"],
            "created":         ticket["created"],
            "solucao_proposta": solucao,
            "tipo":            ticket["type"],
        }

        # Verifica se tem correspondencia estrategica (Miro)
        tem_correspondencia = False
        if estrategia_texto:
            summary_lower = ticket["summary"].lower()
            words = [w for w in summary_lower.split() if len(w) > 4]
            tem_correspondencia = any(w in estrategia_texto.lower() for w in words)

        if tem_correspondencia or not estrategia_texto:
            relatorio.append(entrada)
        else:
            sem_cruzamento.append(entrada)

    # Ordena por prioridade
    ordem_prioridade = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}
    relatorio.sort(key=lambda x: ordem_prioridade.get(x["prioridade"], 5))

    return {
        "miro": {
            "disponivel":    miro_data.get("disponivel", False),
            "modo":          miro_data.get("modo", "api"),
            "frames":        temas,
            "total_cards":   len(cards_miro),
            "total_itens":   miro_data.get("total_items", 0),
        },
        "jira": {
            "projeto":        jira_project,
            "desde":          since_date,
            "total_tickets":  len(tickets),
        },
        "relatorio":          relatorio,
        "sem_cruzamento":     sem_cruzamento,
        "total_priorizados":  len(relatorio),
    }


# ============================================================
# MÓDULO 10 — Releases e Janelas de Entrega
# ============================================================

def _find_version(project_key: str, version_name_or_id: str) -> dict:
    """Resolve versao pelo nome (parcial, case-insensitive) ou ID numerico."""
    versions = requests.get(
        f"{JIRA_BASE}/project/{project_key}/versions",
        auth=_auth(), headers={"Accept": "application/json"},
    ).json()
    if isinstance(versions, list):
        needle = str(version_name_or_id).lower()
        # Tenta match exato de ID primeiro
        for v in versions:
            if str(v.get("id", "")) == str(version_name_or_id):
                return v
        # Match parcial no nome
        for v in versions:
            if needle in v.get("name", "").lower():
                return v
    return {}


def get_version_report(project_key: str, version_name_or_id: str) -> dict:
    """Relatorio completo de uma janela de entrega (Fix Version).

    Retorna: version_info, issues_vinculados, concluidos, em_aberto,
             arrastados (abertos apos a releaseDate), taxa_conclusao.
    """
    version = _find_version(project_key, version_name_or_id)
    if not version:
        return {"erro": f"Versao '{version_name_or_id}' nao encontrada no projeto {project_key}"}

    vid   = version["id"]
    vname = version.get("name", version_name_or_id)

    # Contadores via API de relatedIssueCounts
    try:
        counts = requests.get(
            f"{JIRA_BASE}/version/{vid}/relatedIssueCounts",
            auth=_auth(), headers={"Accept": "application/json"},
        ).json()
        issues_fixed    = counts.get("issuesFixedCount", 0)
        issues_affected = counts.get("issuesAffectedCount", 0)
    except Exception:
        issues_fixed = issues_affected = 0

    # Issues detalhados
    jql = f'project = {project_key} AND fixVersion = "{vname}"'
    issues = _search(jql, "summary,status,assignee,issuetype,priority,created", max_results=200)

    done_statuses = {"done", "concluido", "concluído", "em producao", "em produção",
                     "finalizado", "closed", "resolved"}
    concluidos = []
    em_aberto  = []
    for issue in issues:
        cat = issue["fields"]["status"]["statusCategory"]["key"]
        if cat == "done":
            concluidos.append(issue["key"])
        else:
            em_aberto.append(issue["key"])

    total = len(issues)
    taxa  = round(len(concluidos) / total * 100, 1) if total else 0

    # Arrastados: abertos e versao ja passou da releaseDate
    release_date = version.get("releaseDate", "")
    today_str    = date.today().isoformat()
    arrastados   = em_aberto if (release_date and release_date < today_str) else []

    return {
        "version": {
            "id":           vid,
            "name":         vname,
            "released":     version.get("released", False),
            "archived":     version.get("archived", False),
            "startDate":    version.get("startDate", "—"),
            "releaseDate":  release_date or "—",
        },
        "total_issues":     total,
        "concluidos":       concluidos,
        "em_aberto":        em_aberto,
        "arrastados":       arrastados,
        "taxa_conclusao":   f"{taxa}%",
        "issues_fixed_api": issues_fixed,
        "detalhes": [
            {
                "key":       i["key"],
                "summary":   i["fields"].get("summary", "")[:80],
                "status":    i["fields"]["status"]["name"],
                "tipo":      i["fields"]["issuetype"]["name"],
                "assignee":  (i["fields"].get("assignee") or {}).get("displayName", "—"),
            }
            for i in issues
        ],
    }


def get_release_history(project_key: str, num_versions: int = 6) -> list:
    """Historico das ultimas N versoes do projeto com metricas consolidadas.

    Retorna lista [{name, startDate, releaseDate, released, issuesFixed,
                    issuesOpen, taxa_conclusao}] ordenada da mais recente para a mais antiga.
    """
    versions = requests.get(
        f"{JIRA_BASE}/project/{project_key}/versions",
        auth=_auth(), headers={"Accept": "application/json"},
    ).json()
    if not isinstance(versions, list):
        return []

    # Ordena por releaseDate decrescente (sem data vai para o final)
    versions_sorted = sorted(
        versions,
        key=lambda v: v.get("releaseDate") or v.get("startDate") or "0000",
        reverse=True,
    )[:num_versions]

    result = []
    for v in versions_sorted:
        vid = v["id"]
        try:
            counts = requests.get(
                f"{JIRA_BASE}/version/{vid}/relatedIssueCounts",
                auth=_auth(), headers={"Accept": "application/json"},
            ).json()
            fixed = counts.get("issuesFixedCount", 0)
            total_rel = fixed + counts.get("issuesUnresolvedCount", 0)
            taxa = round(fixed / total_rel * 100, 1) if total_rel else 0
        except Exception:
            fixed = total_rel = taxa = 0

        result.append({
            "name":          v.get("name", ""),
            "startDate":     v.get("startDate", "—"),
            "releaseDate":   v.get("releaseDate", "—"),
            "released":      v.get("released", False),
            "archived":      v.get("archived", False),
            "issuesFixed":   fixed,
            "issuesTotal":   total_rel,
            "taxa_conclusao": f"{taxa}%",
        })
    return result


def compare_versions(project_key: str,
                     version_name_1: str,
                     version_name_2: str) -> dict:
    """Compara duas janelas de entrega lado a lado.

    Retorna: v1, v2, delta_concluidos, delta_abertos, delta_taxa.
    """
    v1 = get_version_report(project_key, version_name_1)
    v2 = get_version_report(project_key, version_name_2)
    if "erro" in v1 or "erro" in v2:
        return {"v1": v1, "v2": v2}

    t1 = float(v1["taxa_conclusao"].replace("%", "") or 0)
    t2 = float(v2["taxa_conclusao"].replace("%", "") or 0)

    return {
        "v1":              v1,
        "v2":              v2,
        "delta_concluidos": len(v2["concluidos"]) - len(v1["concluidos"]),
        "delta_abertos":    len(v2["em_aberto"])  - len(v1["em_aberto"]),
        "delta_taxa":       round(t2 - t1, 1),
    }


# ============================================================
# MÓDULO 11 — SLA e Análise de Suporte (projeto SUP)
# ============================================================

_FINAL_STATUSES = {"finalizado", "done", "concluido", "concluído",
                   "closed", "resolved", "fechado"}


def _issue_resolution_hours(issue_key: str) -> float:
    """Retorna horas entre criacao e primeiro status final via changelog."""
    try:
        data = _get(f"/issue/{issue_key}",
                    params={"expand": "changelog",
                            "fields": "created,status"})
        created = _parse_dt(data["fields"].get("created"))
        if not created:
            return 0.0
        for hist in sorted(data["changelog"]["histories"],
                           key=lambda h: h["created"]):
            for item in hist["items"]:
                if item["field"] == "status":
                    if item.get("toString", "").lower() in _FINAL_STATUSES:
                        resolved = _parse_dt(hist["created"])
                        if resolved and resolved > created:
                            return (resolved - created).total_seconds() / 3600
    except Exception:
        pass
    return 0.0


def get_support_sla(days: int = 30, project_key: str = "SUP") -> dict:
    """Tempo medio de resolucao por tipo de issue no projeto de suporte.

    Retorna: {por_tipo: [{tipo, total, resolvidos, tempo_medio_horas,
                          tempo_medio_dias}], media_geral_dias}
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    jql   = (f'project = {project_key} AND created >= "{since}" '
             f'AND issuetype in standardIssueTypes()')
    issues = _search(jql, "summary,status,issuetype,created,priority", max_results=200)

    by_type = defaultdict(lambda: {"total": 0, "resolvidos": 0,
                                   "horas_resolucao": [], "keys": []})
    for issue in issues:
        f    = issue["fields"]
        tipo = (f.get("issuetype") or {}).get("name", "Outros")
        cat  = f["status"]["statusCategory"]["key"]
        by_type[tipo]["total"]  += 1
        by_type[tipo]["keys"].append(issue["key"])
        if cat == "done":
            by_type[tipo]["resolvidos"] += 1

    # Calcular tempo de resolucao por tipo (batch de keys resolvidas)
    for tipo, data_t in by_type.items():
        resolved_keys = [
            issues[i]["key"]
            for i in range(len(issues))
            if issues[i]["fields"]["issuetype"].get("name") == tipo
            and issues[i]["fields"]["status"]["statusCategory"]["key"] == "done"
        ][:20]  # limita para nao sobrecarregar API
        for key in resolved_keys:
            h = _issue_resolution_hours(key)
            if h > 0:
                data_t["horas_resolucao"].append(h)

    por_tipo = []
    all_hours = []
    for tipo, d in by_type.items():
        avg_h = round(mean(d["horas_resolucao"]), 1) if d["horas_resolucao"] else None
        all_hours.extend(d["horas_resolucao"])
        por_tipo.append({
            "tipo":              tipo,
            "total":             d["total"],
            "resolvidos":        d["resolvidos"],
            "tempo_medio_horas": avg_h,
            "tempo_medio_dias":  round(avg_h / 24, 1) if avg_h else None,
        })

    por_tipo.sort(key=lambda x: -x["total"])
    media_geral = round(mean(all_hours) / 24, 1) if all_hours else None

    return {
        "projeto":         project_key,
        "periodo_dias":    days,
        "por_tipo":        por_tipo,
        "media_geral_dias": media_geral,
        "total_analisados": len(issues),
    }


def get_escalated_tickets(days: int = 30) -> dict:
    """Tickets com label jira_escalated em aberto, ordenados por tempo em aberto."""
    since = (date.today() - timedelta(days=days)).isoformat()
    jql   = (f'labels = jira_escalated '
             f'AND status not in ("Finalizado","Done","Concluido","Concluído","Fechado") '
             f'AND created >= "{since}" '
             f'ORDER BY created ASC')
    issues = _search(jql, "summary,status,assignee,created,priority,issuetype,labels",
                     max_results=100)
    today_dt = datetime.now(timezone.utc)
    rows = []
    for issue in issues:
        f       = issue["fields"]
        created = _parse_dt(f.get("created"))
        dias    = round((today_dt - created).total_seconds() / 86400, 1) if created else 0
        rows.append({
            "key":           issue["key"],
            "summary":       f.get("summary", "")[:80],
            "status":        f["status"]["name"],
            "assignee":      (f.get("assignee") or {}).get("displayName", "—"),
            "priority":      (f.get("priority") or {}).get("name", "—"),
            "issuetype":     (f.get("issuetype") or {}).get("name", ""),
            "created":       f.get("created", "")[:10],
            "dias_em_aberto": dias,
        })
    rows.sort(key=lambda x: -x["dias_em_aberto"])
    return {
        "total":         len(rows),
        "periodo_dias":  days,
        "tickets":       rows,
    }


def get_support_by_client(client_name: str, days: int = 90) -> dict:
    """Volume e metricas de atendimento por cliente no projeto SUP.

    Retorna: {cliente, total, por_tipo, por_status, tempo_medio_resolucao_dias, tickets[]}
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    jql   = (f'project = SUP AND text ~ "{client_name}" '
             f'AND created >= "{since}" ORDER BY created DESC')
    issues = _search(jql, "summary,status,assignee,issuetype,priority,created", max_results=100)

    by_tipo   = defaultdict(int)
    by_status = defaultdict(int)
    tickets   = []
    resolved_keys = []

    for issue in issues:
        f    = issue["fields"]
        tipo = (f.get("issuetype") or {}).get("name", "?")
        stat = f["status"]["name"]
        cat  = f["status"]["statusCategory"]["key"]
        by_tipo[tipo]   += 1
        by_status[stat] += 1
        tickets.append({
            "key":      issue["key"],
            "summary":  f.get("summary", "")[:80],
            "tipo":     tipo,
            "status":   stat,
            "created":  f.get("created", "")[:10],
            "assignee": (f.get("assignee") or {}).get("displayName", "—"),
        })
        if cat == "done":
            resolved_keys.append(issue["key"])

    hours = [_issue_resolution_hours(k) for k in resolved_keys[:15]]
    hours = [h for h in hours if h > 0]
    avg_days = round(mean(hours) / 24, 1) if hours else None

    return {
        "cliente":                  client_name,
        "total":                    len(issues),
        "periodo_dias":             days,
        "por_tipo":                 dict(by_tipo),
        "por_status":               dict(by_status),
        "tempo_medio_resolucao_dias": avg_days,
        "tickets":                  tickets,
    }


def get_reopen_rate(project_key: str = "SUP", days: int = 90) -> dict:
    """Taxa de reabertura: issues que voltaram de status final para aberto.

    Retorna: {total_analisados, total_reabertos, taxa_reabertura_pct, casos[]}
    """
    since = (date.today() - timedelta(days=days)).isoformat()
    jql   = (f'project = {project_key} AND created >= "{since}" '
             f'AND issuetype in standardIssueTypes()')
    issues = _search(jql, "summary,status,issuetype", max_results=200)

    casos = []
    for issue in issues:
        try:
            data = _get(f"/issue/{issue['key']}",
                        params={"expand": "changelog", "fields": "summary,status"})
            prev_final = False
            for hist in sorted(data["changelog"]["histories"],
                               key=lambda h: h["created"]):
                for item in hist["items"]:
                    if item["field"] != "status":
                        continue
                    from_s = item.get("fromString", "").lower()
                    to_s   = item.get("toString",   "").lower()
                    if from_s in _FINAL_STATUSES:
                        prev_final = True
                    elif prev_final and to_s not in _FINAL_STATUSES:
                        casos.append({
                            "key":         issue["key"],
                            "summary":     data["fields"].get("summary", "")[:70],
                            "reaberto_em": hist["created"][:10],
                            "reaberto_por": (hist.get("author") or {}).get("displayName", "?"),
                            "de_status":    item.get("fromString", ""),
                            "para_status":  item.get("toString", ""),
                        })
                        prev_final = False
        except Exception:
            pass

    total = len(issues)
    reabertos = len({c["key"] for c in casos})
    taxa = round(reabertos / total * 100, 1) if total else 0

    return {
        "projeto":           project_key,
        "periodo_dias":      days,
        "total_analisados":  total,
        "total_reabertos":   reabertos,
        "taxa_reabertura_pct": taxa,
        "casos":             casos,
    }


# ============================================================
# MÓDULO 12 — Dependências e Bloqueios Cross-project
# ============================================================

_BLOCK_LINK_TYPES = {
    "is blocked by", "bloqueado por", "blocked by",
    "is blocking", "bloqueia", "blocks",
    "depends on", "depende de",
}


def get_blocked_issues(project_keys: list = None) -> list:
    """Issues bloqueados (com link do tipo 'is blocked by') em projetos abertos.

    Limita a 50 issues abertos por projeto para nao sobrecarregar a API.
    """
    projects = project_keys or PROJECTS
    jql = (f'project in ({",".join(projects)}) '
           f'AND status not in ("Done","Concluido","Concluído","Finalizado","Fechado","Em Producao","Em Produção") '
           f'AND issuetype in standardIssueTypes() '
           f'AND issuetype not in subTaskIssueTypes() '
           f'ORDER BY updated DESC')
    issues = _search(jql, "summary,status,assignee,issuelinks", max_results=50)

    blocked = []
    for issue in issues:
        f    = issue["fields"]
        links = f.get("issuelinks") or []
        bloqueadores = []
        for link in links:
            tipo = (link.get("type") or {}).get("name", "").lower()
            inward_name = (link.get("type") or {}).get("inward", "").lower()
            if tipo in _BLOCK_LINK_TYPES or inward_name in _BLOCK_LINK_TYPES:
                blocker = link.get("inwardIssue") or link.get("outwardIssue") or {}
                if blocker:
                    bloqueadores.append({
                        "key":     blocker.get("key", ""),
                        "summary": (blocker.get("fields") or {}).get("summary", "")[:60],
                        "status":  ((blocker.get("fields") or {}).get("status") or {}).get("name", ""),
                    })
        if bloqueadores:
            blocked.append({
                "key":          issue["key"],
                "summary":      f.get("summary", "")[:80],
                "status":       f["status"]["name"],
                "assignee":     (f.get("assignee") or {}).get("displayName", "—"),
                "bloqueado_por": bloqueadores,
            })
    return blocked


def get_dependency_map(epic_key: str) -> dict:
    """Grafo de dependencias de um epico (1 nivel de profundidade).

    Retorna: {epicKey, links_diretos, links_indiretos}
    """
    try:
        data = _get(f"/issue/{epic_key}",
                    params={"fields": "summary,status,issuelinks,issuetype"})
    except Exception as e:
        return {"erro": str(e)}

    f       = data["fields"]
    diretos = []
    indirect_keys = []

    for link in (f.get("issuelinks") or []):
        tipo_nome = (link.get("type") or {}).get("name", "")
        related   = link.get("inwardIssue") or link.get("outwardIssue") or {}
        if not related:
            continue
        rf = related.get("fields") or {}
        entry = {
            "key":       related.get("key", ""),
            "summary":   rf.get("summary", "")[:70],
            "tipo_link": tipo_nome,
            "status":    (rf.get("status") or {}).get("name", ""),
            "projeto":   related.get("key", "")[:related.get("key", "").find("-")],
        }
        diretos.append(entry)
        indirect_keys.append(related.get("key", ""))

    # Nivel 2: links dos filhos diretos (apenas resume, nao recursivo)
    indiretos = []
    for key in indirect_keys[:10]:  # limita para nao sobrecarregar
        try:
            d2 = _get(f"/issue/{key}",
                      params={"fields": "issuelinks"})
            for link in (d2["fields"].get("issuelinks") or []):
                rel = link.get("inwardIssue") or link.get("outwardIssue") or {}
                rkey = rel.get("key", "")
                if rkey and rkey != epic_key and rkey not in indirect_keys:
                    rf = rel.get("fields") or {}
                    indiretos.append({
                        "key":       rkey,
                        "summary":   rf.get("summary", "")[:70],
                        "tipo_link": (link.get("type") or {}).get("name", ""),
                        "via":       key,
                        "projeto":   rkey[:rkey.find("-")] if "-" in rkey else "",
                    })
        except Exception:
            pass

    return {
        "epicKey":        epic_key,
        "summary":        f.get("summary", ""),
        "links_diretos":  diretos,
        "links_indiretos": indiretos,
    }


def get_cross_project_links(project_key: str) -> dict:
    """Vinculos externos de um projeto (links para outros projetos).

    Retorna: {total_links_externos, por_projeto_destino, detalhes}
    """
    jql = (f'project = {project_key} '
           f'AND status not in ("Done","Concluido","Concluído","Finalizado","Fechado") '
           f'AND issuetype in standardIssueTypes()')
    issues = _search(jql, "summary,status,issuelinks", max_results=100)

    by_dest   = defaultdict(lambda: {"total": 0, "tipos": defaultdict(int)})
    detalhes  = []

    for issue in issues:
        for link in (issue["fields"].get("issuelinks") or []):
            rel  = link.get("inwardIssue") or link.get("outwardIssue") or {}
            rkey = rel.get("key", "")
            if not rkey:
                continue
            dest_proj = rkey[:rkey.find("-")] if "-" in rkey else ""
            if dest_proj and dest_proj != project_key:
                tipo = (link.get("type") or {}).get("name", "relates to")
                by_dest[dest_proj]["total"] += 1
                by_dest[dest_proj]["tipos"][tipo] += 1
                detalhes.append({
                    "from":      issue["key"],
                    "to":        rkey,
                    "tipo_link": tipo,
                    "summary_from": issue["fields"].get("summary", "")[:60],
                    "summary_to":   (rel.get("fields") or {}).get("summary", "")[:60],
                })

    por_projeto = [
        {"projeto": k, "total": v["total"],
         "tipos_link": dict(v["tipos"])}
        for k, v in sorted(by_dest.items(), key=lambda x: -x[1]["total"])
    ]

    return {
        "projeto_origem":       project_key,
        "total_links_externos": len(detalhes),
        "por_projeto_destino":  por_projeto,
        "detalhes":             detalhes[:50],
    }


# ============================================================
# MÓDULO 13 — Análise de Qualidade e Bugs
# ============================================================

def get_bug_rate_by_epic(project_keys: list = None, days: int = 180) -> list:
    """Bugs agrupados por epico pai, ordenados por total de bugs desc.

    Retorna: [{epic_key, epic_summary, total_bugs, abertos, concluidos, bugs[]}]
    """
    projects = project_keys or PROJECTS
    since    = (date.today() - timedelta(days=days)).isoformat()
    jql = (f'project in ({",".join(projects)}) '
           f'AND issuetype = Bug AND created >= "{since}"')
    bugs = _search(jql, "summary,status,assignee,issuetype,parent,created,priority",
                   max_results=300)

    by_epic = defaultdict(lambda: {
        "epic_summary": "",
        "total": 0, "abertos": 0, "concluidos": 0, "bugs": [],
    })

    for bug in bugs:
        f      = bug["fields"]
        parent = f.get("parent") or {}
        # Tenta pegar epico via parent
        epic_key = parent.get("key", "SEM_EPICO")
        epic_sum = (parent.get("fields") or {}).get("summary", "Sem Epico")

        cat = f["status"]["statusCategory"]["key"]
        by_epic[epic_key]["epic_summary"] = epic_sum
        by_epic[epic_key]["total"]       += 1
        if cat == "done":
            by_epic[epic_key]["concluidos"] += 1
        else:
            by_epic[epic_key]["abertos"] += 1
        by_epic[epic_key]["bugs"].append({
            "key":     bug["key"],
            "summary": f.get("summary", "")[:60],
            "status":  f["status"]["name"],
            "created": f.get("created", "")[:10],
        })

    result = [
        {"epic_key": k, **v}
        for k, v in by_epic.items()
    ]
    result.sort(key=lambda x: -x["total"])
    return result


def get_bug_trend(project_keys: list = None, months: int = 6) -> list:
    """Evolucao mensal de abertura vs fechamento de bugs.

    Retorna: [{mes, criados, resolvidos, saldo}]
    Saldo positivo = acumulando divida de bugs.
    """
    projects = project_keys or PROJECTS
    today    = date.today()
    rows     = []

    for i in range(months - 1, -1, -1):
        # Primeiro dia do mes relativo
        first = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        # Ultimo dia do mes
        if first.month == 12:
            last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)

        criados = len(_search(
            f'project in ({",".join(projects)}) AND issuetype = Bug '
            f'AND created >= "{first.isoformat()}" AND created <= "{last.isoformat()}"',
            "summary", max_results=500
        ))
        resolvidos = len(_search(
            f'project in ({",".join(projects)}) AND issuetype = Bug '
            f'AND statusCategory = Done '
            f'AND updated >= "{first.isoformat()}" AND updated <= "{last.isoformat()}"',
            "summary", max_results=500
        ))
        rows.append({
            "mes":        first.strftime("%Y/%m"),
            "criados":    criados,
            "resolvidos": resolvidos,
            "saldo":      criados - resolvidos,
        })
    return rows


def get_post_release_bugs(project_key: str, version_name: str) -> dict:
    """Bugs abertos APOS o lancamento de uma versao especifica.

    Retorna: {version, release_date, total_bugs_pos_release, bugs[]}
    """
    version = _find_version(project_key, version_name)
    if not version:
        return {"erro": f"Versao '{version_name}' nao encontrada em {project_key}"}

    release_date = version.get("releaseDate", "")
    vname        = version.get("name", version_name)

    if not release_date:
        return {
            "version":      vname,
            "aviso":        "Versao sem releaseDate definida — impossivel calcular bugs pos-release",
            "bugs":         [],
        }

    # Bugs criados apos o lancamento mencionando a versao no texto
    jql_text = (f'project = {project_key} AND issuetype = Bug '
                f'AND created >= "{release_date}" '
                f'AND text ~ "{vname}" ORDER BY created DESC')
    bugs_text = _search(jql_text, "summary,status,assignee,created,priority", max_results=50)

    # Bugs com affectsVersion = versao
    jql_aff = (f'project = {project_key} AND issuetype = Bug '
               f'AND affectedVersion = "{vname}" ORDER BY created DESC')
    bugs_aff = _search(jql_aff, "summary,status,assignee,created,priority", max_results=50)

    # Union por key
    seen = set()
    all_bugs = []
    for bug in bugs_text + bugs_aff:
        if bug["key"] not in seen:
            seen.add(bug["key"])
            f = bug["fields"]
            all_bugs.append({
                "key":      bug["key"],
                "summary":  f.get("summary", "")[:80],
                "status":   f["status"]["name"],
                "assignee": (f.get("assignee") or {}).get("displayName", "—"),
                "created":  f.get("created", "")[:10],
                "priority": (f.get("priority") or {}).get("name", "—"),
            })

    all_bugs.sort(key=lambda x: x["created"])
    return {
        "version":               vname,
        "release_date":          release_date,
        "total_bugs_pos_release": len(all_bugs),
        "bugs":                  all_bugs,
    }


# ============================================================
# MÓDULO 14 — Capacidade e Planejamento de Sprint
# ============================================================

def get_capacity_forecast(board_id: int, num_sprints: int = 5) -> dict:
    """Projecao de capacidade baseada nos ultimos N sprints fechados.

    Retorna: {media_stories, media_points, sugestao_conservadora,
              sugestao_otimista, intervalo_confianca_80pct, historico_sprints}
    """
    velocity = get_sprint_velocity(board_id, num_sprints)
    historico = velocity.get("historico", [])
    if not historico:
        return {"erro": f"Nenhum sprint fechado encontrado no board {board_id}"}

    stories_list = [h["historias_concluidas"] for h in historico]
    points_list  = [h["story_points"]         for h in historico]

    avg_st  = round(mean(stories_list), 1)
    avg_pts = round(mean(points_list), 1)
    std_st  = round(stdev(stories_list), 1) if len(stories_list) > 1 else 0
    std_pts = round(stdev(points_list), 1)  if len(points_list) > 1 else 0

    # Intervalo de confianca 80% ~ media +/- 1.28 * dp
    z80 = 1.28
    return {
        "board_id":               board_id,
        "sprints_analisados":     len(historico),
        "media_stories":          avg_st,
        "media_points":           avg_pts,
        "sugestao_conservadora":  {
            "stories": max(0, round(avg_st - std_st)),
            "points":  max(0, round(avg_pts - std_pts)),
        },
        "sugestao_otimista":      {
            "stories": round(avg_st),
            "points":  round(avg_pts),
        },
        "intervalo_confianca_80pct": {
            "stories_min": max(0, round(avg_st - z80 * std_st)),
            "stories_max": round(avg_st + z80 * std_st),
            "points_min":  max(0, round(avg_pts - z80 * std_pts)),
            "points_max":  round(avg_pts + z80 * std_pts),
        },
        "desvio_padrao_stories": std_st,
        "desvio_padrao_points":  std_pts,
        "historico_sprints":     historico,
    }


def get_backlog_readiness(board_id: int, max_results: int = 50) -> dict:
    """Historias no backlog que NAO estao prontas para entrar no sprint.

    Verifica: estimativa, responsavel, descricao, criterio de aceite.
    Retorna: {total_backlog, prontas, nao_prontas, por_problema, issues_nao_prontos}
    """
    jql = (f'board = {board_id} '
           f'AND statusCategory = new '
           f'AND issuetype in standardIssueTypes() '
           f'AND issuetype not in subTaskIssueTypes() '
           f'AND sprint is EMPTY')
    issues = _search(jql,
                     "summary,status,assignee,customfield_10016,description,priority",
                     max_results=max_results)

    sem_estimativa  = []
    sem_responsavel = []
    sem_descricao   = []
    sem_criterio    = []
    prontas_keys    = []
    nao_prontas     = []

    for issue in issues:
        f       = issue["fields"]
        problemas = []

        pts = _pts(f.get("customfield_10016"))
        if pts == 0:
            problemas.append("sem_estimativa")
            sem_estimativa.append(issue["key"])

        if not f.get("assignee"):
            problemas.append("sem_responsavel")
            sem_responsavel.append(issue["key"])

        desc_text = _adf_to_text(f.get("description"))
        if not desc_text or len(desc_text.strip()) < 30:
            problemas.append("sem_descricao")
            sem_descricao.append(issue["key"])
        elif not any(kw in desc_text.lower()
                     for kw in ["ac:", "criterio", "critério", "acceptance",
                                "aceite", "dado que", "when", "então"]):
            problemas.append("sem_criterio_aceite")
            sem_criterio.append(issue["key"])

        if problemas:
            nao_prontas.append({
                "key":      issue["key"],
                "summary":  f.get("summary", "")[:70],
                "problemas": problemas,
                "points":   pts,
                "assignee": (f.get("assignee") or {}).get("displayName", "—"),
            })
        else:
            prontas_keys.append(issue["key"])

    return {
        "total_backlog":    len(issues),
        "prontas":          len(prontas_keys),
        "nao_prontas":      len(nao_prontas),
        "pct_prontas":      round(len(prontas_keys) / len(issues) * 100, 1) if issues else 0,
        "por_problema": {
            "sem_estimativa":    len(sem_estimativa),
            "sem_responsavel":   len(sem_responsavel),
            "sem_descricao":     len(sem_descricao),
            "sem_criterio_aceite": len(sem_criterio),
        },
        "issues_nao_prontos": nao_prontas,
        "keys_prontas":       prontas_keys,
    }


def simulate_sprint(board_id: int, story_keys: list) -> dict:
    """Estima risco de conclusao de um conjunto de historias baseado no historico dos devs.

    Retorna: {stories_analisadas, pontos_total, risco_geral, por_historia[]}
    """
    # Busca metricas dos devs (ultimos 5 sprints)
    try:
        dev_metrics = get_developer_metrics(board_id, num_sprints=5)
    except Exception:
        dev_metrics = {}

    _confianca_score = {"ALTA": 90, "MEDIA": 72, "BAIXA": 55, "CRITICA": 35, "SEM_DADOS": 65}

    por_historia = []
    pontos_total = 0.0

    for key in story_keys:
        try:
            data = _get(f"/issue/{key}",
                        params={"fields": "summary,status,assignee,customfield_10016,issuetype"})
            f       = data["fields"]
            pts     = _pts(f.get("customfield_10016"))
            assignee = (f.get("assignee") or {}).get("displayName", "—")
            pontos_total += pts

            dev_data   = dev_metrics.get(assignee, {})
            confianca  = dev_data.get("confianca_entrega", "SEM_DADOS")
            taxa_str   = dev_data.get("taxa_entrega_media", "—")
            taxa_num   = float(taxa_str.replace("%", "")) if taxa_str != "—" else 65.0
            score      = _confianca_score.get(confianca, 65)

            if taxa_num >= 85 and score >= 85:
                risco_ind = "BAIXO"
            elif taxa_num >= 70:
                risco_ind = "MEDIO"
            elif taxa_num >= 50:
                risco_ind = "ALTO"
            else:
                risco_ind = "CRITICO"

            por_historia.append({
                "key":           key,
                "summary":       f.get("summary", "")[:70],
                "assignee":      assignee,
                "points":        pts,
                "confianca_dev": confianca,
                "taxa_entrega":  taxa_str,
                "risco_individual": risco_ind,
            })
        except Exception as e:
            por_historia.append({"key": key, "erro": str(e)})

    # Risco geral = pior risco individual
    niveis = ["CRITICO", "ALTO", "MEDIO", "BAIXO"]
    risco_geral = "BAIXO"
    for h in por_historia:
        r = h.get("risco_individual", "BAIXO")
        if niveis.index(r) < niveis.index(risco_geral):
            risco_geral = r

    return {
        "board_id":          board_id,
        "stories_analisadas": len(por_historia),
        "pontos_total":       pontos_total,
        "risco_geral":        risco_geral,
        "por_historia":       por_historia,
    }


# ============================================================
# MÓDULO 15 — Relatórios Exportáveis
# ============================================================

def _get_downloads_dir() -> str:
    """Retorna o caminho da pasta Downloads do usuario."""
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _build_docx_base(title: str, subtitle: str = ""):
    """Helper: cria Document padrao e retorna (doc, add_heading_fn, add_table_fn)."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except ImportError:
        raise ImportError("python-docx nao instalado. Execute: pip install python-docx")

    doc = Document()

    # Titulo
    tp = doc.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = tp.add_run(title)
    tr.bold = True
    tr.font.size = Pt(16)
    tr.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

    if subtitle:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = sp.add_run(subtitle)
        sr.font.size = Pt(10)
        sr.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

    doc.add_paragraph()

    def add_heading(text, level=1):
        p = doc.add_heading(text, level=level)
        for run in p.runs:
            run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
        return p

    def add_table(headers, rows):
        table = doc.add_table(rows=1 + len(rows), cols=len(headers))
        table.style = "Table Grid"
        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            hdr_cells[i].text = h
            run = hdr_cells[i].paragraphs[0].runs[0]
            run.bold = True
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            tc = hdr_cells[i]._tc
            tcPr = tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), "1F4E79")
            shd.set(qn("w:val"), "clear")
            tcPr.append(shd)
        for r_idx, row_data in enumerate(rows):
            rcells = table.rows[r_idx + 1].cells
            for c_idx, cell_text in enumerate(row_data):
                rcells[c_idx].text = str(cell_text)
                rcells[c_idx].paragraphs[0].runs[0].font.size = Pt(10)
                if r_idx % 2 == 1:
                    tc = rcells[c_idx]._tc
                    tcPr = tc.get_or_add_tcPr()
                    shd = OxmlElement("w:shd")
                    shd.set(qn("w:fill"), "D6E4F0")
                    shd.set(qn("w:val"), "clear")
                    tcPr.append(shd)
        return table

    return doc, add_heading, add_table


def export_sprint_report(board_id: int, sprint_id: int = None,
                         output_dir: str = None) -> dict:
    """Gera relatorio de sprint em Word (.docx).

    Retorna: {'path': caminho_do_arquivo, 'gerado_em': datetime_str}
    """
    report  = get_sprint_report(board_id, sprint_id)
    details = get_sprint_stories_detail(board_id, sprint_id)

    sprint_name = report.get("sprint", f"Sprint board {board_id}")
    subtitle    = f"Gerado em: {date.today().strftime('%d/%m/%Y')} | {report.get('periodo', '')}"
    doc, add_heading, add_table = _build_docx_base(
        f"Relatorio de Sprint — {sprint_name}", subtitle
    )

    add_heading("Resumo do Sprint")
    add_table(
        ["Campo", "Valor"],
        [
            ["Sprint",               report.get("sprint", "—")],
            ["Estado",               report.get("estado", "—")],
            ["Periodo",              report.get("periodo", "—")],
            ["Total de Historias",   str(report.get("total_historias", 0))],
            ["Concluidas",           str(report.get("concluidas", 0))],
            ["Em Andamento",         str(report.get("em_andamento", 0))],
            ["Nao Iniciadas",        str(report.get("nao_iniciadas", 0))],
            ["Taxa de Conclusao",    report.get("taxa_conclusao", "—")],
            ["Story Points Total",   str(report.get("story_points_total", 0))],
            ["Story Points Concluidos", str(report.get("story_points_concluidos", 0))],
        ]
    )
    doc.add_paragraph()

    if details:
        add_heading("Historias — Detalhe e Risco")
        rows = [
            [
                d.get("key", ""),
                d.get("summary", "")[:60],
                d.get("status", ""),
                d.get("assignee", ""),
                d.get("risk", ""),
                d.get("tempo_no_status", ""),
                d.get("overrun", ""),
            ]
            for d in details
        ]
        add_table(
            ["Ticket", "Resumo", "Status", "Responsavel", "Risco", "Tempo Status", "Overrun"],
            rows
        )

    out_dir  = output_dir or _get_downloads_dir()
    filename = f"Sprint_{sprint_name.replace('/', '-').replace(' ', '_')}.docx"
    path     = os.path.join(out_dir, filename)
    doc.save(path)
    return {"path": path, "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M")}


def export_epic_summary(epic_key: str, output_dir: str = None) -> dict:
    """Gera resumo de epico em Word.

    Retorna: {'path': caminho_do_arquivo}
    """
    progress = get_epic_progress(epic_key)
    prs_data = get_issue_prs(epic_key)

    try:
        epic_data = _get(f"/issue/{epic_key}",
                         params={"fields": "summary,status,assignee,fixVersions,"
                                           "customfield_11336,customfield_11337"})
        ef       = epic_data["fields"]
        title    = ef.get("summary", epic_key)
        status   = ef["status"]["name"]
        assignee = (ef.get("assignee") or {}).get("displayName", "—")
        fix_v    = [v.get("name", "") for v in (ef.get("fixVersions") or [])]
    except Exception:
        title = epic_key; status = "?"; assignee = "—"; fix_v = []

    subtitle = f"Epico: {epic_key} | Gerado em: {date.today().strftime('%d/%m/%Y')}"
    doc, add_heading, add_table = _build_docx_base(title, subtitle)

    add_heading("Dados do Epico")
    add_table(
        ["Campo", "Valor"],
        [
            ["Chave",           epic_key],
            ["Status",          status],
            ["Responsavel",     assignee],
            ["Janela/Versao",   ", ".join(fix_v) if fix_v else "—"],
            ["Total Historias", str(progress.get("total_historias", 0))],
            ["Concluidas",      str(progress.get("concluidas", 0))],
            ["% Conclusao",     progress.get("percentual_conclusao", "—")],
            ["Story Points",    f'{progress.get("story_points_concluidos", 0)} / '
                                f'{progress.get("story_points_total", 0)}'],
        ]
    )
    doc.add_paragraph()

    if progress.get("por_status"):
        add_heading("Historias por Status")
        add_table(
            ["Status", "Quantidade"],
            [[k, str(v)] for k, v in progress["por_status"].items()]
        )
        doc.add_paragraph()

    prs = prs_data.get("pull_requests", []) + [
        l for l in prs_data.get("remote_links", []) if l.get("is_pr")
    ]
    if prs:
        add_heading("Pull Requests Relacionados")
        add_table(
            ["Titulo/URL", "Status", "Repo"],
            [[p.get("title") or p.get("url", "")[:60], p.get("status", ""), p.get("repo", "")]
             for p in prs[:10]]
        )

    out_dir  = output_dir or _get_downloads_dir()
    filename = f"Epico_{epic_key}.docx"
    path     = os.path.join(out_dir, filename)
    doc.save(path)
    return {"path": path}


def export_version_report(project_key: str, version_name: str,
                          output_dir: str = None) -> dict:
    """Gera relatorio de janela de entrega em Word.

    Retorna: {'path': caminho_do_arquivo}
    """
    vr = get_version_report(project_key, version_name)
    if "erro" in vr:
        return vr

    vinfo    = vr.get("version", {})
    vname    = vinfo.get("name", version_name)
    subtitle = (f"Projeto: {project_key} | "
                f"Janela: {vinfo.get('releaseDate', '—')} | "
                f"Gerado em: {date.today().strftime('%d/%m/%Y')}")
    doc, add_heading, add_table = _build_docx_base(f"Relatorio de Janela — {vname}", subtitle)

    add_heading("Sumario Executivo")
    add_table(
        ["Campo", "Valor"],
        [
            ["Versao",        vname],
            ["Status",        "Lancada" if vinfo.get("released") else "Nao lancada"],
            ["Data Inicio",   vinfo.get("startDate", "—")],
            ["Data Lancamento", vinfo.get("releaseDate", "—")],
            ["Total Issues",  str(vr.get("total_issues", 0))],
            ["Concluidos",    str(len(vr.get("concluidos", [])))],
            ["Em Aberto",     str(len(vr.get("em_aberto", [])))],
            ["Arrastados",    str(len(vr.get("arrastados", [])))],
            ["Taxa Conclusao", vr.get("taxa_conclusao", "—")],
        ]
    )
    doc.add_paragraph()

    detalhes = vr.get("detalhes", [])
    if detalhes:
        add_heading("Issues da Janela")
        add_table(
            ["Ticket", "Resumo", "Tipo", "Status", "Responsavel"],
            [[d["key"], d["summary"], d["tipo"], d["status"], d["assignee"]]
             for d in detalhes]
        )
        doc.add_paragraph()

    arrastados = vr.get("arrastados", [])
    if arrastados:
        add_heading("Issues Arrastados (nao concluidos apos a data de lancamento)")
        doc.add_paragraph(", ".join(arrastados))

    out_dir  = output_dir or _get_downloads_dir()
    filename = f"Janela_{project_key}_{vname.replace('/', '-').replace(' ', '_')}.docx"
    path     = os.path.join(out_dir, filename)
    doc.save(path)
    return {"path": path, "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M")}


# ============================================================
# MÓDULO 16 — Labels e Áreas Temáticas
# ============================================================

def get_issues_by_label(label: str, days: int = 90,
                        project_keys: list = None) -> dict:
    """Issues agrupados por uma label especifica.

    Retorna: {label, total, abertos, fechados, por_tipo, por_status,
              por_responsavel, tempo_medio_resolucao_dias, issues[]}
    """
    projects = project_keys or PROJECTS
    since    = (date.today() - timedelta(days=days)).isoformat()
    proj_jql = f'project in ({",".join(projects)}) AND ' if projects else ""
    jql      = (f'{proj_jql}labels = "{label}" AND created >= "{since}" '
                f'ORDER BY created DESC')
    issues   = _search(jql, "summary,status,assignee,issuetype,created,priority",
                       max_results=200)

    by_tipo   = defaultdict(int)
    by_status = defaultdict(int)
    by_resp   = defaultdict(int)
    abertos = fechados = 0
    resolved_keys = []
    rows = []

    for issue in issues:
        f    = issue["fields"]
        tipo = (f.get("issuetype") or {}).get("name", "?")
        stat = f["status"]["name"]
        cat  = f["status"]["statusCategory"]["key"]
        resp = (f.get("assignee") or {}).get("displayName", "—")
        by_tipo[tipo]   += 1
        by_status[stat] += 1
        by_resp[resp]   += 1
        if cat == "done":
            fechados += 1
            resolved_keys.append(issue["key"])
        else:
            abertos += 1
        rows.append({
            "key":     issue["key"],
            "summary": f.get("summary", "")[:70],
            "status":  stat,
            "tipo":    tipo,
            "created": f.get("created", "")[:10],
            "assignee": resp,
        })

    hours = [_issue_resolution_hours(k) for k in resolved_keys[:15]]
    hours = [h for h in hours if h > 0]
    avg_days = round(mean(hours) / 24, 1) if hours else None

    return {
        "label":                     label,
        "total":                     len(issues),
        "abertos":                   abertos,
        "fechados":                  fechados,
        "periodo_dias":              days,
        "por_tipo":                  dict(by_tipo),
        "por_status":                dict(by_status),
        "por_responsavel":           dict(sorted(by_resp.items(),
                                                  key=lambda x: -x[1])[:10]),
        "tempo_medio_resolucao_dias": avg_days,
        "issues":                    rows,
    }


def get_label_trend(label: str, months: int = 6) -> list:
    """Evolucao mensal de issues com uma label especifica.

    Retorna: [{mes, criados, resolvidos, saldo}]
    """
    today = date.today()
    rows  = []

    for i in range(months - 1, -1, -1):
        first = (today.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        if first.month == 12:
            last = first.replace(year=first.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = first.replace(month=first.month + 1, day=1) - timedelta(days=1)

        criados = len(_search(
            f'labels = "{label}" AND created >= "{first.isoformat()}" '
            f'AND created <= "{last.isoformat()}"',
            "summary", max_results=500
        ))
        resolvidos = len(_search(
            f'labels = "{label}" AND statusCategory = Done '
            f'AND updated >= "{first.isoformat()}" AND updated <= "{last.isoformat()}"',
            "summary", max_results=500
        ))
        rows.append({
            "mes":        first.strftime("%Y/%m"),
            "criados":    criados,
            "resolvidos": resolvidos,
            "saldo":      criados - resolvidos,
        })
    return rows


def get_top_labels(project_keys: list = None, days: int = 180,
                   top_n: int = 15) -> list:
    """Ranking das labels mais usadas nos projetos monitorados.

    Retorna: [{label, total, abertos, fechados}] top N ordenados por total desc.
    """
    projects = project_keys or PROJECTS
    since    = (date.today() - timedelta(days=days)).isoformat()
    jql      = (f'project in ({",".join(projects)}) AND created >= "{since}" '
                f'AND labels is not EMPTY ORDER BY created DESC')
    issues   = _search(jql, "labels,status", max_results=500)

    label_counts = defaultdict(lambda: {"total": 0, "abertos": 0, "fechados": 0})
    for issue in issues:
        f   = issue["fields"]
        cat = f["status"]["statusCategory"]["key"]
        for lbl in (f.get("labels") or []):
            label_counts[lbl]["total"] += 1
            if cat == "done":
                label_counts[lbl]["fechados"] += 1
            else:
                label_counts[lbl]["abertos"] += 1

    result = [
        {"label": k, **v}
        for k, v in label_counts.items()
    ]
    result.sort(key=lambda x: -x["total"])
    return result[:top_n]


# ============================================================
# MELHORIA TRANSVERSAL — Detecção de Contexto de Suporte
# ============================================================

_SUP_STOPWORDS = {
    "o", "a", "os", "as", "um", "uma", "de", "do", "da", "e", "ou",
    "em", "no", "na", "que", "com", "para", "por", "se", "nao",
    "esta", "foi", "tem", "como", "mas", "problema", "erro", "issue",
    "the", "is", "in", "on", "with", "for", "and", "or",
}
_DEV_PROJECT_PREFIXES = {"TPROJ", "TSRV", "TNP", "TLIGHTCOM", "TLIGHTDIST",
                         "THP", "TTRD", "PROJTHUN", "TVAR"}


def find_support_context(natural_query: str, days: int = 90) -> dict:
    """Dado um texto em linguagem natural mencionando cliente + sintoma,
    busca automaticamente tickets SUP relacionados e rastreia o ticket de dev correspondente.

    Fluxo:
    1. Extrai termos relevantes da query (remove stopwords)
    2. Busca no SUP com os termos extraidos
    3. Para cada SUP, busca ticket de dev via issuelinks ou chave no texto
    4. Para tickets de dev encontrados, chama get_issue_prs para rastreabilidade de codigo
    5. Retorna: {sup_tickets, dev_tickets, prs_relacionados, termos_usados}

    Exemplo:
      "cliente Statkraft com problema no relatorio Quantity Price"
      → busca SUP com "Statkraft" e "Quantity"
      → encontra SUP-17788, SUP-18061
      → rastreia para TPROJ e TSRV relacionados
    """
    # 1. Extrair termos relevantes
    tokens = re.sub(r"[^\w\sáéíóúâêôãõàçüñ]", " ", natural_query,
                    flags=re.U).split()
    termos = [t for t in tokens
              if len(t) >= 4 and t.lower() not in _SUP_STOPWORDS]
    if not termos:
        return {"erro": "Nao foi possivel extrair termos relevantes da query."}

    # 2. Busca no SUP — usa os 2 termos mais longos como ancora
    anchors = sorted(set(termos), key=len, reverse=True)[:2]
    text_clauses = " AND ".join(f'text ~ "{t}"' for t in anchors)
    since        = (date.today() - timedelta(days=days)).isoformat()
    jql_sup      = (f'project = SUP AND ({text_clauses}) '
                    f'AND created >= "{since}" ORDER BY created DESC')

    sup_raw = _search(jql_sup,
                      "summary,status,assignee,issuetype,created,priority,issuelinks",
                      max_results=20)

    sup_tickets = []
    dev_keys_seen = set()
    dev_keys = []

    for issue in sup_raw:
        f    = issue["fields"]
        sup_tickets.append({
            "key":      issue["key"],
            "summary":  f.get("summary", "")[:80],
            "status":   f["status"]["name"],
            "tipo":     (f.get("issuetype") or {}).get("name", ""),
            "assignee": (f.get("assignee") or {}).get("displayName", "—"),
            "created":  f.get("created", "")[:10],
            "priority": (f.get("priority") or {}).get("name", "—"),
        })

        # 3. Rastrear tickets de dev via issuelinks
        for link in (f.get("issuelinks") or []):
            rel  = link.get("inwardIssue") or link.get("outwardIssue") or {}
            rkey = rel.get("key", "")
            prefix = rkey[:rkey.find("-")] if "-" in rkey else ""
            if prefix in _DEV_PROJECT_PREFIXES and rkey not in dev_keys_seen:
                dev_keys_seen.add(rkey)
                dev_keys.append(rkey)

    # 4. Busca textual por chaves de dev nas descricoes dos tickets SUP
    _key_re = re.compile(
        r'\b(' + '|'.join(p for p in _DEV_PROJECT_PREFIXES) + r')-\d+\b'
    )
    for issue in sup_raw:
        try:
            full = _get(f"/issue/{issue['key']}",
                        params={"fields": "description,comment"})
            ff = full["fields"]
            text_blob = _adf_to_text(ff.get("description")) + " "
            for c in (ff.get("comment") or {}).get("comments", []):
                text_blob += _adf_to_text(c.get("body")) + " "
            for m in _key_re.finditer(text_blob):
                k = m.group(0)
                if k not in dev_keys_seen:
                    dev_keys_seen.add(k)
                    dev_keys.append(k)
        except Exception:
            pass

    # 5. Buscar info dos tickets de dev e PRs relacionados
    dev_tickets    = []
    prs_relacionados = []

    for key in dev_keys[:10]:  # limita para nao sobrecarregar
        try:
            d = _get(f"/issue/{key}",
                     params={"fields": "summary,status,assignee,issuetype,fixVersions,parent"})
            df = d["fields"]
            dev_tickets.append({
                "key":       key,
                "summary":   df.get("summary", "")[:80],
                "status":    df["status"]["name"],
                "tipo":      (df.get("issuetype") or {}).get("name", ""),
                "assignee":  (df.get("assignee") or {}).get("displayName", "—"),
                "fix_versions": [v.get("name", "") for v in (df.get("fixVersions") or [])],
            })
            # PRs via Jira
            prs = get_issue_prs(key)
            all_prs = prs.get("pull_requests", []) + prs.get("remote_links", [])
            if all_prs:
                prs_relacionados.append({
                    "issue_key":     key,
                    "pull_requests": all_prs[:5],
                    "gmud_links":    [l for l in prs.get("remote_links", [])
                                      if l.get("type") == "gmud"],
                })
        except Exception:
            pass

    return {
        "query":           natural_query,
        "termos_usados":   anchors,
        "periodo_dias":    days,
        "sup_tickets":     sup_tickets,
        "dev_tickets":     dev_tickets,
        "prs_relacionados": prs_relacionados,
        "total_sup":       len(sup_tickets),
        "total_dev":       len(dev_tickets),
    }
