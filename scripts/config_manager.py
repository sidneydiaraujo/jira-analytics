"""
Gerenciador de configuracao do usuario para jira-analytics.
Armazena preferencias persistentes: boards, devs e assuntos de interesse.

Config salva em: ~/.claude/jira-analytics-user-config.json
"""
import json
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "jira-analytics-user-config.json"

_DEFAULT = {
    "default_board": 86,
    "boards": {
        "projetos": 86,
        "dev projetos": 86,
        "[dev] projetos": 86,
    },
    "developers": [],
    "topics": [],
}


# ---------------------------------------------------------------------------
# Leitura / escrita
# ---------------------------------------------------------------------------

def load() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return dict(_DEFAULT)


def save(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

def resolve_board(name_or_id) -> int | None:
    """Resolve nome ou ID de board para o ID numerico.

    Aceita: inteiro, string numerica, alias cadastrado (busca parcial, case-insensitive).
    Retorna None se nao encontrar.
    """
    if isinstance(name_or_id, int):
        return name_or_id
    try:
        return int(name_or_id)
    except (ValueError, TypeError):
        pass
    config = load()
    key = str(name_or_id).lower().strip()
    boards = config.get("boards", {})
    if key in boards:
        return boards[key]
    for alias, bid in boards.items():
        if key in alias or alias in key:
            return bid
    return None


def get_default_board() -> int:
    return load().get("default_board", 86)


def add_board(alias: str, board_id: int) -> dict:
    config = load()
    config.setdefault("boards", {})[alias.lower().strip()] = board_id
    save(config)
    return config


def remove_board(alias: str) -> dict:
    config = load()
    config.get("boards", {}).pop(alias.lower().strip(), None)
    save(config)
    return config


def set_default_board(board_id: int) -> dict:
    config = load()
    config["default_board"] = board_id
    save(config)
    return config


# ---------------------------------------------------------------------------
# Desenvolvedores
# ---------------------------------------------------------------------------

def get_developers() -> list:
    return load().get("developers", [])


def add_developer(name: str) -> dict:
    config = load()
    devs = config.setdefault("developers", [])
    if not any(d.lower() == name.lower() for d in devs):
        devs.append(name)
        save(config)
    return config


def remove_developer(name: str) -> dict:
    config = load()
    config["developers"] = [d for d in config.get("developers", [])
                            if d.lower() != name.lower()]
    save(config)
    return config


# ---------------------------------------------------------------------------
# Assuntos de interesse
# ---------------------------------------------------------------------------

def get_topics() -> list:
    return load().get("topics", [])


def add_topic(topic: str) -> dict:
    config = load()
    topics = config.setdefault("topics", [])
    if topic.lower() not in [t.lower() for t in topics]:
        topics.append(topic)
        save(config)
    return config


def remove_topic(topic: str) -> dict:
    config = load()
    config["topics"] = [t for t in config.get("topics", [])
                        if t.lower() != topic.lower()]
    save(config)
    return config


# ---------------------------------------------------------------------------
# Exibicao
# ---------------------------------------------------------------------------

def show_config() -> str:
    config = load()
    lines = ["=== Configuracao jira-analytics ==="]

    default_board = config.get("default_board")
    lines.append(f"\nBoard padrao: {default_board}")

    boards = config.get("boards", {})
    if boards:
        lines.append("\nBoards configurados:")
        for alias, bid in boards.items():
            mark = "  <- padrao" if bid == default_board else ""
            lines.append(f"  '{alias}' -> board {bid}{mark}")

    devs = config.get("developers", [])
    lines.append(f"\nDesenvolvedores: {', '.join(devs) if devs else '(nenhum)'}")

    topics = config.get("topics", [])
    lines.append(f"Assuntos: {', '.join(topics) if topics else '(nenhum)'}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    args = sys.argv[1:]

    if not args or "--list" in args or "-l" in args:
        print(show_config())
        return

    if "--add-board" in args:
        i = args.index("--add-board")
        alias  = args[i + 1]
        bid    = int(args[i + 2])
        add_board(alias, bid)
        print(f"Board '{alias}' -> {bid} adicionado.")
        return

    if "--remove-board" in args:
        i = args.index("--remove-board")
        remove_board(args[i + 1])
        print(f"Board '{args[i+1]}' removido.")
        return

    if "--default-board" in args:
        i = args.index("--default-board")
        set_default_board(int(args[i + 1]))
        print(f"Board padrao definido: {args[i+1]}")
        return

    if "--add-dev" in args:
        i = args.index("--add-dev")
        add_developer(args[i + 1])
        print(f"Desenvolvedor '{args[i+1]}' adicionado.")
        return

    if "--remove-dev" in args:
        i = args.index("--remove-dev")
        remove_developer(args[i + 1])
        print(f"Desenvolvedor '{args[i+1]}' removido.")
        return

    if "--add-topic" in args:
        i = args.index("--add-topic")
        add_topic(args[i + 1])
        print(f"Assunto '{args[i+1]}' adicionado.")
        return

    if "--remove-topic" in args:
        i = args.index("--remove-topic")
        remove_topic(args[i + 1])
        print(f"Assunto '{args[i+1]}' removido.")
        return

    print("Uso:")
    print("  python config_manager.py --list")
    print("  python config_manager.py --add-board 'Nome' <id>")
    print("  python config_manager.py --remove-board 'Nome'")
    print("  python config_manager.py --default-board <id>")
    print("  python config_manager.py --add-dev 'Nome'")
    print("  python config_manager.py --remove-dev 'Nome'")
    print("  python config_manager.py --add-topic 'assunto'")
    print("  python config_manager.py --remove-topic 'assunto'")


if __name__ == "__main__":
    _cli()
