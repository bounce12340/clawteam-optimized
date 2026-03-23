"""Heartbeat system for ClawTeam - agents report status periodically."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clawteam.team.models import get_data_dir


@dataclass
class HeartbeatRecord:
    """Single heartbeat record from an agent."""
    agent_name: str
    timestamp: str  # ISO format
    status: str  # "idle", "working", "error", "completed"
    current_task: Optional[str] = None
    progress_percent: int = 0
    message: str = ""


class HeartbeatManager:
    """Manager for agent heartbeat reporting and monitoring.
    
    Agents should call report() every 30 seconds to indicate they are alive.
    If an agent hasn't reported within timeout_seconds, it is considered stale.
    """
    
    DEFAULT_TIMEOUT = 60  # seconds
    HEARTBEAT_INTERVAL = 30  # seconds - how often agents should report
    
    def __init__(self, team_name: str):
        self.team_name = team_name
        self._heartbeat_dir = self._get_heartbeat_dir()
        self._heartbeat_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_heartbeat_dir(self) -> Path:
        return get_data_dir() / "heartbeat" / self.team_name
    
    def _get_heartbeat_path(self, agent_name: str) -> Path:
        return self._heartbeat_dir / f"{agent_name}.json"
    
    def report(
        self,
        agent_name: str,
        status: str = "working",
        current_task: Optional[str] = None,
        progress_percent: int = 0,
        message: str = "",
    ) -> None:
        """Record a heartbeat from an agent.
        
        Args:
            agent_name: Name of the reporting agent
            status: Current status - "idle", "working", "error", "completed"
            current_task: ID of current task being worked on
            progress_percent: Progress percentage (0-100)
            message: Optional status message
        """
        record = HeartbeatRecord(
            agent_name=agent_name,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status=status,
            current_task=current_task,
            progress_percent=progress_percent,
            message=message,
        )
        
        path = self._get_heartbeat_path(agent_name)
        path.write_text(
            json.dumps(asdict(record), indent=2),
            encoding="utf-8",
        )
    
    def get_last_heartbeat(self, agent_name: str) -> Optional[HeartbeatRecord]:
        """Get the last heartbeat record for an agent."""
        path = self._get_heartbeat_path(agent_name)
        if not path.exists():
            return None
        
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return HeartbeatRecord(**data)
        except (json.JSONDecodeError, TypeError):
            return None
    
    def is_alive(self, agent_name: str, timeout_seconds: int = DEFAULT_TIMEOUT) -> bool:
        """Check if an agent is alive based on its last heartbeat.
        
        Returns True if the agent has reported within timeout_seconds.
        """
        record = self.get_last_heartbeat(agent_name)
        if not record:
            return False
        
        try:
            last_time = datetime.fromisoformat(record.timestamp)
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            return elapsed < timeout_seconds
        except (ValueError, TypeError):
            return False
    
    def get_stale_agents(
        self,
        agent_names: list[str],
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> list[str]:
        """Get list of agents that haven't reported within timeout.
        
        Args:
            agent_names: List of agent names to check
            timeout_seconds: Timeout in seconds
            
        Returns:
            List of agent names that are stale (not responding)
        """
        stale = []
        for name in agent_names:
            if not self.is_alive(name, timeout_seconds):
                stale.append(name)
        return stale
    
    def get_all_statuses(self) -> dict[str, Optional[HeartbeatRecord]]:
        """Get heartbeat status for all agents in the team."""
        statuses = {}
        for path in self._heartbeat_dir.glob("*.json"):
            agent_name = path.stem
            statuses[agent_name] = self.get_last_heartbeat(agent_name)
        return statuses
    
    def get_team_health(self, agent_names: list[str]) -> dict:
        """Get overall team health summary.
        
        Returns dict with:
        - total: total number of agents
        - alive: number of alive agents
        - stale: number of stale agents
        - working: number of agents currently working
        - idle: number of idle agents
        - error: number of agents in error state
        """
        alive_count = 0
        stale_count = 0
        working_count = 0
        idle_count = 0
        error_count = 0
        
        for name in agent_names:
            record = self.get_last_heartbeat(name)
            if self.is_alive(name):
                alive_count += 1
                if record:
                    if record.status == "working":
                        working_count += 1
                    elif record.status == "idle":
                        idle_count += 1
                    elif record.status == "error":
                        error_count += 1
            else:
                stale_count += 1
        
        return {
            "total": len(agent_names),
            "alive": alive_count,
            "stale": stale_count,
            "working": working_count,
            "idle": idle_count,
            "error": error_count,
            "healthy": stale_count == 0 and error_count == 0,
        }


def send_heartbeat(
    team_name: str,
    agent_name: str,
    status: str = "working",
    current_task: Optional[str] = None,
    progress_percent: int = 0,
    message: str = "",
) -> None:
    """Convenience function to send a heartbeat.
    
    Agents should call this every HEARTBEAT_INTERVAL seconds (30s).
    """
    manager = HeartbeatManager(team_name)
    manager.report(agent_name, status, current_task, progress_percent, message)
