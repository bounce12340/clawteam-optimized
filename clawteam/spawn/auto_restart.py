"""AutoRestart - automatically re-spawn dead agents.

Workflow
--------
1. When a team spawns an agent, call ``save_spawn_config()`` to persist
   the full spawn arguments (command, prompt, env, cwd, etc.) to a
   ``spawn_configs.json`` file in the team directory.

2. At any time, call ``check_and_restart()`` to:
   a. Ask SpawnRegistry for dead agents.
   b. For each dead agent that owns in_progress tasks, attempt a re-spawn
      using the saved config.
   c. Return a list of agents that were successfully restarted.

3. Individual restarts can be triggered manually via ``restart_agent()``.

Persistence
-----------
Configs are stored in::

    ~/.clawteam/teams/{team}/spawn_configs.json

Each entry has the keys expected by the spawn backends::

    {
        "worker1": {
            "command": ["claude"],
            "agent_type": "general-purpose",
            "prompt": "...",
            "env": {"MY_VAR": "val"},
            "cwd": "/path/to/worktree",
            "skip_permissions": true,
            "backend": "tmux"
        },
        ...
    }
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from clawteam.team.models import get_data_dir


def _configs_path(team_name: str) -> Path:
    d = get_data_dir() / "teams" / team_name
    d.mkdir(parents=True, exist_ok=True)
    return d / "spawn_configs.json"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_spawn_config(
    team_name: str,
    agent_name: str,
    command: list[str],
    agent_type: str = "general-purpose",
    agent_id: str = "",
    prompt: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    skip_permissions: bool = False,
    backend: str = "tmux",
) -> None:
    """Persist the full spawn configuration so the agent can be restarted."""
    path = _configs_path(team_name)
    configs = _load(path)
    configs[agent_name] = {
        "command": command,
        "agent_type": agent_type,
        "agent_id": agent_id,
        "prompt": prompt,
        "env": env or {},
        "cwd": cwd,
        "skip_permissions": skip_permissions,
        "backend": backend,
    }
    _save(path, configs)


def get_spawn_config(team_name: str, agent_name: str) -> dict[str, Any] | None:
    """Return saved spawn config for *agent_name*, or None if not found."""
    return _load(_configs_path(team_name)).get(agent_name)


def list_spawn_configs(team_name: str) -> dict[str, dict[str, Any]]:
    """Return all saved spawn configs for a team."""
    return _load(_configs_path(team_name))


def delete_spawn_config(team_name: str, agent_name: str) -> None:
    """Remove the saved config for an agent (called on clean exit)."""
    path = _configs_path(team_name)
    configs = _load(path)
    configs.pop(agent_name, None)
    _save(path, configs)


# ---------------------------------------------------------------------------
# Restart logic
# ---------------------------------------------------------------------------

def restart_agent(team_name: str, agent_name: str) -> str:
    """Re-spawn *agent_name* using its saved config.

    Returns a human-readable status message (starts with "OK" or "Error").
    """
    cfg = get_spawn_config(team_name, agent_name)
    if not cfg:
        return f"Error: no saved spawn config for '{agent_name}'"

    backend_name = cfg.get("backend", "tmux")
    command = cfg.get("command") or []
    agent_type = cfg.get("agent_type", "general-purpose")
    agent_id = cfg.get("agent_id", "")
    prompt = cfg.get("prompt")
    env = cfg.get("env") or {}
    cwd = cfg.get("cwd")
    skip_permissions = bool(cfg.get("skip_permissions", False))

    if not command:
        return f"Error: saved config for '{agent_name}' has no command"

    try:
        from clawteam.spawn import get_backend
        backend = get_backend(backend_name)
        result = backend.spawn(
            command=command,
            agent_name=agent_name,
            agent_id=agent_id,
            agent_type=agent_type,
            team_name=team_name,
            prompt=prompt,
            env=env,
            cwd=cwd,
            skip_permissions=skip_permissions,
        )
        return result
    except Exception as exc:
        return f"Error: restart failed for '{agent_name}': {exc}"


def check_and_restart(
    team_name: str,
    *,
    only_if_stuck_tasks: bool = True,
) -> list[str]:
    """Check for dead agents and restart those with pending work.

    Parameters
    ----------
    team_name:
        The team to inspect.
    only_if_stuck_tasks:
        If True (default), only restart agents that own at least one
        ``in_progress`` task.  If False, restart any registered dead agent
        that has a saved spawn config.

    Returns
    -------
    List of agent names that were successfully restarted.
    """
    try:
        from clawteam.spawn.registry import list_dead_agents
    except ImportError:
        return []

    dead = list_dead_agents(team_name)
    if not dead:
        return []

    agents_to_restart = set(dead)

    if only_if_stuck_tasks:
        try:
            from clawteam.team.tasks import TaskStore
            from clawteam.team.models import TaskStatus
            store = TaskStore(team_name)
            tasks = store.list_tasks(status=TaskStatus.in_progress)
            owners_with_work = {t.owner for t in tasks if t.owner}
            agents_to_restart &= owners_with_work
        except Exception:
            pass

    restarted: list[str] = []
    for agent_name in sorted(agents_to_restart):
        result = restart_agent(team_name, agent_name)
        if result.startswith("OK") or "Spawned" in result or "spawned" in result:
            restarted.append(agent_name)

    return restarted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(path: Path, data: dict) -> None:
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
