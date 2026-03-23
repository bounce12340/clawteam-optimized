"""Timeout detection and recovery for ClawTeam tasks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from clawteam.team.models import TaskStatus, get_data_dir
from clawteam.team.tasks import TaskStore


class TimeoutWatcher:
    """Watches for tasks that have been in progress for too long.
    
    Monitors task durations and can:
    - Detect stuck tasks (in_progress for too long)
    - Alert on slow tasks
    - Auto-retry or escalate stuck tasks
    """
    
    DEFAULT_TASK_TIMEOUT = 600  # 10 minutes
    DEFAULT_ALERT_THRESHOLD = 300  # 5 minutes - warn before timeout
    
    def __init__(self, team_name: str):
        self.team_name = team_name
        self.task_store = TaskStore(team_name)
        self._timeout_config = self._load_timeout_config()
    
    def _get_config_path(self) -> Path:
        return get_data_dir() / "timeout_config" / f"{self.team_name}.json"
    
    def _load_timeout_config(self) -> dict:
        """Load timeout configuration for this team."""
        path = self._get_config_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "default_timeout": self.DEFAULT_TASK_TIMEOUT,
            "alert_threshold": self.DEFAULT_ALERT_THRESHOLD,
            "task_timeouts": {},  # per-task overrides
        }
    
    def _save_timeout_config(self) -> None:
        """Save timeout configuration."""
        path = self._get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._timeout_config, indent=2),
            encoding="utf-8",
        )
    
    def set_task_timeout(self, task_id: str, timeout_seconds: int) -> None:
        """Set a custom timeout for a specific task."""
        self._timeout_config["task_timeouts"][task_id] = timeout_seconds
        self._save_timeout_config()
    
    def get_task_timeout(self, task_id: str) -> int:
        """Get the timeout for a task (custom or default)."""
        return self._timeout_config["task_timeouts"].get(
            task_id,
            self._timeout_config["default_timeout"],
        )
    
    def _get_task_duration(self, task) -> Optional[float]:
        """Calculate how long a task has been in progress (seconds)."""
        if not task.started_at:
            return None
        
        try:
            start_time = datetime.fromisoformat(task.started_at)
            return (datetime.now(timezone.utc) - start_time).total_seconds()
        except (ValueError, TypeError):
            return None
    
    def check_task(self, task_id: str) -> dict:
        """Check a specific task for timeout conditions.
        
        Returns dict with:
        - task_id: task ID
        - status: "ok", "alert", "timeout"
        - duration_seconds: how long task has been running
        - timeout_seconds: timeout threshold
        - message: human-readable status
        """
        task = self.task_store.get(task_id)
        if not task:
            return {
                "task_id": task_id,
                "status": "error",
                "message": "Task not found",
            }
        
        if task.status != TaskStatus.in_progress:
            return {
                "task_id": task_id,
                "status": "ok",
                "message": f"Task is {task.status.value}",
            }
        
        duration = self._get_task_duration(task)
        if duration is None:
            return {
                "task_id": task_id,
                "status": "error",
                "message": "Cannot determine task duration",
            }
        
        timeout = self.get_task_timeout(task_id)
        alert_threshold = self._timeout_config["alert_threshold"]
        
        if duration > timeout:
            return {
                "task_id": task_id,
                "status": "timeout",
                "duration_seconds": round(duration, 2),
                "timeout_seconds": timeout,
                "message": f"Task timeout: {duration:.0f}s > {timeout}s",
            }
        elif duration > alert_threshold:
            return {
                "task_id": task_id,
                "status": "alert",
                "duration_seconds": round(duration, 2),
                "timeout_seconds": timeout,
                "message": f"Task slow: {duration:.0f}s (threshold: {alert_threshold}s)",
            }
        else:
            return {
                "task_id": task_id,
                "status": "ok",
                "duration_seconds": round(duration, 2),
                "timeout_seconds": timeout,
                "message": f"Task progressing: {duration:.0f}s",
            }
    
    def check_all_in_progress(self) -> list[dict]:
        """Check all in-progress tasks for timeouts.
        
        Returns list of status dicts for each in-progress task.
        """
        tasks = self.task_store.list_tasks(status=TaskStatus.in_progress)
        return [self.check_task(task.id) for task in tasks]
    
    def get_stuck_tasks(self) -> list[dict]:
        """Get list of tasks that have timed out."""
        all_statuses = self.check_all_in_progress()
        return [s for s in all_statuses if s["status"] == "timeout"]
    
    def get_slow_tasks(self) -> list[dict]:
        """Get list of tasks that are slow but not yet timed out."""
        all_statuses = self.check_all_in_progress()
        return [s for s in all_statuses if s["status"] == "alert"]
    
    def auto_release_stuck_tasks(self, max_retries: int = 1) -> list[str]:
        """Automatically release locks on stuck tasks.
        
        For tasks that have timed out, release their locks so other
        agents can take over.
        
        Args:
            max_retries: Maximum number of times to retry a stuck task
            
        Returns:
            List of task IDs that were released
        """
        stuck = self.get_stuck_tasks()
        released = []
        
        for status in stuck:
            task_id = status["task_id"]
            task = self.task_store.get(task_id)
            
            if not task:
                continue
            
            # Check retry count
            retries = task.metadata.get("timeout_retries", 0)
            if retries >= max_retries:
                # Mark as failed instead of retrying
                self.task_store.update(
                    task_id,
                    status=TaskStatus.pending,
                    caller="timeout_watcher",
                    metadata={"timeout_retries": retries + 1, "timeout_failed": True},
                )
            else:
                # Release lock and mark for retry
                self.task_store.update(
                    task_id,
                    status=TaskStatus.pending,
                    caller="timeout_watcher",
                    metadata={"timeout_retries": retries + 1},
                )
            
            released.append(task_id)
        
        return released
    
    def get_timeout_summary(self) -> dict:
        """Get summary of timeout status for the team.
        
        Returns dict with:
        - total_in_progress: number of in-progress tasks
        - ok: number of tasks progressing normally
        - alert: number of slow tasks
        - timeout: number of timed out tasks
        """
        all_statuses = self.check_all_in_progress()
        
        return {
            "total_in_progress": len(all_statuses),
            "ok": sum(1 for s in all_statuses if s["status"] == "ok"),
            "alert": sum(1 for s in all_statuses if s["status"] == "alert"),
            "timeout": sum(1 for s in all_statuses if s["status"] == "timeout"),
        }
