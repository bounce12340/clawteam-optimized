"""Health monitoring dashboard for ClawTeam."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clawteam.team.models import get_data_dir, TaskStatus
from clawteam.team.tasks import TaskStore
from clawteam.heartbeat import HeartbeatManager
from clawteam.timeout import TimeoutWatcher
from clawteam.recovery import AutoRestart
from clawteam.spawn.registry import get_registry, is_agent_alive


@dataclass
class AgentHealth:
    """Health status for a single agent."""
    name: str
    alive: bool
    status: str  # "idle", "working", "error", "completed", "unknown"
    current_task: Optional[str] = None
    progress: int = 0
    last_heartbeat: Optional[str] = None
    restart_count: int = 0
    process_status: str = "unknown"  # "running", "exited", "unknown"


@dataclass
class TaskHealth:
    """Health status for a single task."""
    id: str
    subject: str
    status: str
    owner: Optional[str] = None
    duration_seconds: Optional[float] = None
    timeout_status: str = "ok"  # "ok", "alert", "timeout"


class HealthMonitor:
    """Comprehensive health monitoring for a team.
    
    Combines data from:
    - HeartbeatManager (agent liveness)
    - TaskStore (task status)
    - TimeoutWatcher (task timeouts)
    - AutoRestart (restart history)
    - Spawn Registry (process status)
    
    Provides a unified health dashboard.
    """
    
    def __init__(self, team_name: str):
        self.team_name = team_name
        self.task_store = TaskStore(team_name)
        self.heartbeat = HeartbeatManager(team_name)
        self.timeout_watcher = TimeoutWatcher(team_name)
        self.recovery = AutoRestart(team_name)
    
    def get_agent_health(self, agent_name: str) -> AgentHealth:
        """Get comprehensive health status for an agent."""
        # Check process liveness
        alive = is_agent_alive(self.team_name, agent_name)
        process_status = "unknown"
        if alive is True:
            process_status = "running"
        elif alive is False:
            process_status = "exited"
        
        # Get heartbeat info
        heartbeat_record = self.heartbeat.get_last_heartbeat(agent_name)
        
        # Get restart count
        restart_count = self.recovery.get_restart_count(agent_name)
        
        # Determine agent status
        if heartbeat_record:
            # Use heartbeat status if available
            status = heartbeat_record.status
            # If process exited but heartbeat says completed, that's normal
            if process_status == "exited" and status == "completed":
                alive = True  # Consider completed agents as healthy
            elif process_status == "exited":
                alive = False
            else:
                alive = alive and self.heartbeat.is_alive(agent_name)
            
            return AgentHealth(
                name=agent_name,
                alive=alive,
                status=status,
                current_task=heartbeat_record.current_task,
                progress=heartbeat_record.progress_percent,
                last_heartbeat=heartbeat_record.timestamp,
                restart_count=restart_count,
                process_status=process_status,
            )
        else:
            # No heartbeat - rely on process status only
            return AgentHealth(
                name=agent_name,
                alive=alive if alive is True else False,
                status="completed" if process_status == "exited" else "unknown",
                current_task=None,
                progress=0,
                last_heartbeat=None,
                restart_count=restart_count,
                process_status=process_status,
            )
    
    def get_all_agents_health(self) -> list[AgentHealth]:
        """Get health status for all agents in the team."""
        registry = get_registry(self.team_name)
        return [self.get_agent_health(name) for name in registry.keys()]
    
    def get_task_health(self, task_id: str) -> TaskHealth:
        """Get health status for a task."""
        task = self.task_store.get(task_id)
        if not task:
            return TaskHealth(
                id=task_id,
                subject="Unknown",
                status="not_found",
            )
        
        # Get timeout status
        timeout_info = self.timeout_watcher.check_task(task_id)
        
        # Calculate duration
        duration = None
        if task.started_at:
            try:
                start = datetime.fromisoformat(task.started_at)
                duration = (datetime.now(timezone.utc) - start).total_seconds()
            except (ValueError, TypeError):
                pass
        
        return TaskHealth(
            id=task.id,
            subject=task.subject,
            status=task.status.value,
            owner=task.owner,
            duration_seconds=round(duration, 2) if duration else None,
            timeout_status=timeout_info["status"],
        )
    
    def get_all_tasks_health(self) -> list[TaskHealth]:
        """Get health status for all tasks."""
        tasks = self.task_store.list_tasks()
        return [self.get_task_health(task.id) for task in tasks]
    
    def get_dashboard(self) -> dict:
        """Get comprehensive health dashboard for the team.
        
        Returns dict with:
        - team_name: team name
        - timestamp: when dashboard was generated
        - agents: agent health summary
        - tasks: task health summary
        - overall_health: "healthy", "warning", or "critical"
        - issues: list of current issues
        """
        agents = self.get_all_agents_health()
        tasks = self.get_all_tasks_health()
        
        # Agent summary
        # Count completed agents (exited normally) separately from dead agents
        completed_agents = sum(1 for a in agents if a.status == "completed")
        alive_agents = sum(1 for a in agents if a.alive)
        dead_agents = sum(1 for a in agents if not a.alive and a.status != "completed")
        working_agents = sum(1 for a in agents if a.status == "working")
        error_agents = sum(1 for a in agents if a.status == "error")
        
        # Task summary
        pending = sum(1 for t in tasks if t.status == "pending")
        in_progress = sum(1 for t in tasks if t.status == "in_progress")
        completed = sum(1 for t in tasks if t.status == "completed")
        blocked = sum(1 for t in tasks if t.status == "blocked")
        
        # Timeout issues
        timeout_tasks = [t for t in tasks if t.timeout_status == "timeout"]
        alert_tasks = [t for t in tasks if t.timeout_status == "alert"]
        
        # Determine overall health
        issues = []
        if dead_agents > 0:
            issues.append(f"{dead_agents} dead agents (unexpected)")
        if error_agents > 0:
            issues.append(f"{error_agents} agents in error state")
        if timeout_tasks:
            issues.append(f"{len(timeout_tasks)} timed out tasks")
        if blocked > 0:
            issues.append(f"{blocked} blocked tasks")
        
        # Only count unexpected deaths as critical
        # Completed agents (finished work) are not an issue
        if dead_agents > 0 or error_agents > 0 or timeout_tasks:
            overall = "critical"
        elif alert_tasks or blocked > 0:
            overall = "warning"
        else:
            overall = "healthy"
        
        return {
            "team_name": self.team_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agents": {
                "total": len(agents),
                "alive": alive_agents,
                "dead": dead_agents,
                "completed": completed_agents,
                "working": working_agents,
                "error": error_agents,
                "details": [asdict(a) for a in agents],
            },
            "tasks": {
                "total": len(tasks),
                "pending": pending,
                "in_progress": in_progress,
                "completed": completed,
                "blocked": blocked,
                "timeout": len(timeout_tasks),
                "alert": len(alert_tasks),
                "details": [asdict(t) for t in tasks],
            },
            "overall_health": overall,
            "issues": issues,
        }
    
    def get_quick_status(self) -> str:
        """Get a quick one-line status summary."""
        dashboard = self.get_dashboard()
        
        agents = dashboard["agents"]
        tasks = dashboard["tasks"]
        
        return (
            f"[{dashboard['overall_health'].upper()}] "
            f"Agents: {agents['alive']}/{agents['total']} alive, "
            f"Tasks: {tasks['completed']}/{tasks['total']} done, "
            f"{tasks['in_progress']} in progress"
        )
    
    def export_dashboard(self, output_path: Optional[Path] = None) -> Path:
        """Export dashboard to JSON file.
        
        Returns path to the exported file.
        """
        dashboard = self.get_dashboard()
        
        if output_path is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            output_path = (
                get_data_dir()
                / "health_reports"
                / self.team_name
                / f"dashboard_{timestamp}.json"
            )
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(dashboard, indent=2),
            encoding="utf-8",
        )
        
        return output_path
