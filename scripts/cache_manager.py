"""
cache_manager.py — Cache local em JSON com TTL configuravel.

Usado para resultados lentos: changelog, worklog bulk, epics health.
Nao e importado automaticamente pelo analyzer.py — deve ser chamado
explicitamente quando necessario.

Uso tipico:
    from scripts.cache_manager import get_cached, set_cached
    result = get_cached("get_epics_health", "TPROJ")
    if result is None:
        result = get_epics_health(["TPROJ"])
        set_cached("get_epics_health", result, "TPROJ")
"""
import json
import os
import time
import hashlib
from pathlib import Path

CACHE_DIR = Path.home() / ".claude" / "jira-analytics-cache"
DEFAULT_TTL = 3600  # 1 hora em segundos


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(func_name: str, *args, **kwargs) -> str:
    """Gera chave unica baseada na funcao e argumentos."""
    raw = f"{func_name}|{args}|{sorted(kwargs.items())}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def get_cached(func_name: str, *args, ttl: int = DEFAULT_TTL, **kwargs):
    """Retorna resultado do cache se existir e nao expirou. None caso contrario."""
    key  = _cache_key(func_name, *args, **kwargs)
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
        if time.time() - entry.get("ts", 0) > ttl:
            path.unlink(missing_ok=True)
            return None
        return entry.get("data")
    except Exception:
        return None


def set_cached(func_name: str, result, *args, **kwargs):
    """Salva resultado no cache."""
    _ensure_dir()
    key  = _cache_key(func_name, *args, **kwargs)
    path = _cache_path(key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "func": func_name, "data": result},
                      f, ensure_ascii=False, default=str)
    except Exception:
        pass


def clear_cache(func_name: str = None):
    """Limpa cache de uma funcao especifica ou todo o cache."""
    _ensure_dir()
    if func_name is None:
        for p in CACHE_DIR.glob("*.json"):
            p.unlink(missing_ok=True)
        return {"cleared": "all"}

    cleared = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if entry.get("func") == func_name:
                p.unlink(missing_ok=True)
                cleared += 1
        except Exception:
            pass
    return {"cleared": cleared, "func": func_name}


def cache_stats() -> dict:
    """Retorna estatisticas do cache atual."""
    _ensure_dir()
    entries = list(CACHE_DIR.glob("*.json"))
    if not entries:
        return {"total_entries": 0, "total_size_kb": 0,
                "oldest_entry": None, "newest_entry": None}

    timestamps = []
    total_bytes = 0
    for p in entries:
        try:
            total_bytes += p.stat().st_size
            with open(p, "r", encoding="utf-8") as f:
                entry = json.load(f)
            timestamps.append(entry.get("ts", 0))
        except Exception:
            pass

    def _ts(t):
        import datetime
        return datetime.datetime.fromtimestamp(t).isoformat()[:16] if t else None

    return {
        "total_entries":  len(entries),
        "total_size_kb":  round(total_bytes / 1024, 1),
        "oldest_entry":   _ts(min(timestamps)) if timestamps else None,
        "newest_entry":   _ts(max(timestamps)) if timestamps else None,
    }
