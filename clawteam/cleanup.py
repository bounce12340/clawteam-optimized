"""Cleanup utilities for ClawTeam - manages idle and completed agents.

Uses the spawn registry and heartbeat system for process tracking.
Does NOT require psutil.
"""

from __future__ import annotations

import os
import signal
import subprocess
from datetime import datetime, timezone
from typing import Optional

from clawteam.heartbeat import HeartbeatManager
from clawteam.spawn.registry import get_registry, is_agent_alive, remove_agent
from clawteam.team.manager import TeamManager
from clawteam.team.models import TaskStatus, get_data_dir
from clawteam.team.tasks import TaskStore


def _kill_tmux_window(target: str) -> bool:
    """Kill a tmux window. Returns True on success."""
    if not target:
        return False
    result = subprocess.run(["tmux", "kill-window", "-t", target], capture_output=True)
    return result.returncode == 0


def _kill_pid(pid: int, graceful: bool = True) -> bool:
    """Send SIGTERM (or SIGKILL) to a PID. Returns True if process is gone."""
    if pid <= 0:
        return False
    try:
        if graceful:
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True  # already gone
    except PermissionError:
        return False


def _agent_should_cleanup(
    team_name: str,
    agent_name: str,
    spawn_info: dict,
    idle_threshold_minutes: int = 30,
    idle_only: bool = False,
) -> tuple[bool, str]:
    """Determine whether an agent should be cleaned up.

    Returns (should_cleanup, reason).
    """
    alive = is_agent_alive(team_name, agent_name)

    if alive is False:
        return True, "dead"

    if alive is None:
        # No spawn info — cannot determine
        return False, "no_spawn_info"

    # alive == True
    if idle_only:
        # Check heartbeat status
        hb_mgr = HeartbeatManager(team_name)
        hb = hb_mgr.get_last_heartbeat(agent_name)
        if hb:
            if hb.status == "completed":
                return True, "completed"
            if hb.status == "idle":
                try:
                    last_time = datetime.fromisoformat(hb.timestamp)
                    elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
                    if elapsed >= idle_threshold_minutes * 60:
                        mins = int(elapsed // 60)
                        return True, f"idle_{mins}min"
                except (ValueError, TypeError):
                    pass

        # Check if all tasks are done
        task_store = TaskStore(team_name)
        tasks = task_store.list_tasks(owner=agent_name)
        if tasks and all(t.status in (TaskStatus.completed, TaskStatus.blocked) for t in tasks):
            return True, "all_tasks_done"

        return False, "active"

    # idle_only=False → kill all alive agents
    return True, "force"


def cleanup_team_agents(
    team_name: str,
    idle_only: bool = True,
    idle_threshold_minutes: int = 30,
    dry_run: bool = False,
) -> dict:
    """Cleanup agents for a specific team.

    Args:
        team_name: Name of the team to cleanup
        idle_only: If True, only kill idle/dead/completed agents
        idle_threshold_minutes: Minutes idle before eligible for cleanup
        dry_run: If True, report only — do not kill

    Returns:
        Dict with keys: team, total_agents, killed, skipped, errors
    """
    registry = get_registry(team_name)
    results: dict = {
        "team": team_name,
        "total_agents": len(registry),
        "killed": [],
        "skipped": [],
        "errors": [],
    }

    for agent_name, spawn_info in registry.items():
        should_kill, reason = _agent_should_cleanup(
            team_name, agent_name, spawn_info, idle_threshold_minutes, idle_only
        )

        if not should_kill:
            results["skipped"].append({"agent": agent_name, "reason": reason})
            continue

        if dry_run:
            results["killed"].append({
                "agent": agent_name,
                "pid": spawn_info.get("pid", 0),
                "reason": reason,
                "dry_run": True,
            })
            continue

        # Kill the process
        backend = spawn_info.get("backend", "")
        killed = False
        try:
            if backend == "tmux":
                target = spawn_info.get("tmux_target", "")
                killed = _kill_tmux_window(target)
            elif backend == "subprocess":
                pid = spawn_info.get("pid", 0)
                killed = _kill_pid(pid)
            else:
                killed = True  # nothing to kill

            if killed:
                remove_agent(team_name, agent_name)
                results["killed"].append({
                    "agent": agent_name,
                    "pid": spawn_info.get("pid", 0),
                    "reason": reason,
                    "dry_run": False,
                })
            else:
                results["errors"].append({
                    "agent": agent_name,
                    "error": "kill_failed",
                })
        except Exception as exc:
            results["errors"].append({"agent": agent_name, "error": str(exc)})

    return results


def cleanup_all_teams(
    idle_only: bool = True,
    idle_threshold_minutes: int = 30,
    dry_run: bool = False,
) -> list[dict]:
    """Cleanup agents across all teams with a spawn registry.

    Returns list of cleanup results per team.
    """
    teams_dir = get_data_dir() / "teams"
    results = []
    if not teams_dir.exists():
        return results

    for team_dir in sorted(teams_dir.iterdir()):
        if not team_dir.is_dir():
            continue
        registry_file = team_dir / "spawn_registry.json"
        if not registry_file.exists():
            continue
        team_name = team_dir.name
        result = cleanup_team_agents(
            team_name,
            idle_only=idle_only,
            idle_threshold_minutes=idle_threshold_minutes,
            dry_run=dry_run,
        )
        if result["total_agents"] > 0:
            results.append(result)

    return results


def get_cleanup_summary() -> dict:
    """Get a summary of agents that could be cleaned up across all teams."""
    teams_dir = get_data_dir() / "teams"
    summary: dict = {
        "total_agents": 0,
        "teams": {},
        "idle_agents": [],
    }

    if not teams_dir.exists():
        return summary

    for team_dir in sorted(teams_dir.iterdir()):
        if not team_dir.is_dir():
            continue
        registry_file = team_dir / "spawn_registry.json"
        if not registry_file.exists():
            continue
        team_name = team_dir.name
        registry = get_registry(team_name)
        if not registry:
            continue

        hb_mgr = HeartbeatManager(team_name)
        team_agents = []
        for agent_name, spawn_info in registry.items():
            alive = is_agent_alive(team_name, agent_name)
            hb = hb_mgr.get_last_heartbeat(agent_name)
            agent_entry = {
                "team": team_name,
                "agent": agent_name,
                "pid": spawn_info.get("pid", 0),
                "alive": alive,
                "status": hb.status if hb else "unknown",
            }
            team_agents.append(agent_entry)
            summary["total_agents"] += 1

            should_kill, reason = _agent_should_cleanup(
                team_name, agent_name, spawn_info, 30, idle_only=True
            )
            if should_kill:
                summary["idle_agents"].append({**agent_entry, "reason": reason})

        if team_agents:
            summary["teams"][team_name] = team_agents

    return summary
