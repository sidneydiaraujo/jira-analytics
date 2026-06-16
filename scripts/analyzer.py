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
