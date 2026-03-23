"""Auto-restart functionality for dead agents."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clawteam.team.models import get_data_dir
from clawteam.spawn.registry import get_registry, is_agent_alive, list_dead_agents


class AutoRestart:
    """Automatically restart dead agents.
    
    Monitors agent health and restarts agents that have died.
    Can be configured with restart policies (max retries, backoff, etc.)
    """
    
    DEFAULT_MAX_RESTARTS = 3
    DEFAULT_BACKOFF_SECONDS = 60  # Wait 1 min before restarting
    
    def __init__(self, team_name: str):
        self.team_name = team_name
        self._restart_dir = self._get_restart_dir()
        self._restart_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_restart_dir(self) -> Path:
        return get_data_dir() / "restart" / self.team_name
    
    def _get_restart_path(self, agent_name: str) -> Path:
        return self._restart_dir / f"{agent_name}.json"
    
    def _load_restart_record(self, agent_name: str) -> dict:
        """Load restart history for an agent."""
        path = self._get_restart_path(agent_name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "agent_name": agent_name,
            "restart_count": 0,
            "last_restart": None,
            "restart_history": [],
        }
    
    def _save_restart_record(self, agent_name: str, record: dict) -> None:
        """Save restart history for an agent."""
        path = self._get_restart_path(agent_name)
        path.write_text(
            json.dumps(record, indent=2),
            encoding="utf-8",
        )
    
    def get_restart_count(self, agent_name: str) -> int:
        """Get number of times an agent has been restarted."""
        record = self._load_restart_record(agent_name)
        return record["restart_count"]
    
    def can_restart(self, agent_name: str, max_restarts: int = DEFAULT_MAX_RESTARTS) -> bool:
        """Check if an agent can be restarted (hasn't exceeded max restarts)."""
        return self.get_restart_count(agent_name) < max_restarts
    
    def restart_agent(self, agent_name: str) -> dict:
        """Restart a dead agent.
        
        Returns dict with:
        - success: whether restart succeeded
        - message: status message
        - new_pid: process ID of restarted agent (if success)
        """
        # Get spawn info from registry
        registry = get_registry(self.team_name)
        spawn_info = registry.get(agent_name)
        
        if not spawn_info:
            return {
                "success": False,
                "message": f"No spawn info found for {agent_name}",
            }
        
        # Update restart record
        record = self._load_restart_record(agent_name)
        record["restart_count"] += 1
        record["last_restart"] = datetime.now(timezone.utc).isoformat()
        record["restart_history"].append({
            "timestamp": record["last_restart"],
            "reason": "agent_dead",
        })
        self._save_restart_record(agent_name, record)
        
        # Get the command to restart
        command = spawn_info.get("command", [])
        backend = spawn_info.get("backend", "")
        
        if not command:
            return {
                "success": False,
                "message": f"No restart command found for {agent_name}",
            }
        
        try:
            if backend == "tmux":
                # For tmux, we need to respawn the pane
                tmux_target = spawn_info.get("tmux_target", "")
                if tmux_target:
                    # Kill the existing pane/window
                    subprocess.run(
                        ["tmux", "kill-window", "-t", tmux_target],
                        capture_output=True,
                        check=False,
                    )
                
                # Start new tmux session with the command
                session_name = f"clawteam-{self.team_name}-{agent_name}"
                subprocess.Popen(
                    ["tmux", "new-session", "-d", "-s", session_name, "-n", agent_name] + command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                
                return {
                    "success": True,
                    "message": f"Agent {agent_name} restarted in tmux session {session_name}",
                }
            
            elif backend == "subprocess":
                # For subprocess, just run the command again
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                
                return {
                    "success": True,
                    "message": f"Agent {agent_name} restarted with PID {process.pid}",
                    "new_pid": process.pid,
                }
            
            else:
                return {
                    "success": False,
                    "message": f"Unknown backend: {backend}",
                }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to restart {agent_name}: {e}",
            }
    
    def check_and_restart_all(
        self,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
    ) -> list[dict]:
        """Check all agents and restart any that are dead.
        
        Returns list of restart results for each dead agent.
        """
        dead_agents = list_dead_agents(self.team_name)
        results = []
        
        for agent_name in dead_agents:
            if self.can_restart(agent_name, max_restarts):
                result = self.restart_agent(agent_name)
                results.append({
                    "agent_name": agent_name,
                    "action": "restarted",
                    **result,
                })
            else:
                results.append({
                    "agent_name": agent_name,
                    "action": "skipped",
                    "success": False,
                    "message": f"Max restarts ({max_restarts}) exceeded",
                })
        
        return results
    
    def get_restart_summary(self) -> dict:
        """Get summary of restart status for the team.
        
        Returns dict with:
        - total_agents: total number of agents in registry
        - dead_agents: list of dead agent names
        - restartable: number of agents that can be restarted
        - exceeded_limit: number of agents that exceeded max restarts
        """
        registry = get_registry(self.team_name)
        dead = list_dead_agents(self.team_name)
        
        restartable = 0
        exceeded = 0
        
        for agent_name in dead:
            if self.can_restart(agent_name):
                restartable += 1
            else:
                exceeded += 1
        
        return {
            "total_agents": len(registry),
            "dead_agents": dead,
            "dead_count": len(dead),
            "restartable": restartable,
            "exceeded_limit": exceeded,
        }
