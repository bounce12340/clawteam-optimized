"""TimeoutWatcher - detect stuck tasks and stale heartbeats.

A task is considered *stuck* when:
  - Its status is ``in_progress``
  - Its ``lockedAt`` (or ``startedAt``) timestamp is older than
    ``task_timeout_seconds`` (default: 600 = 10 minutes)

An agent is considered *stale* when:
  - Its heartbeat file exists but its timestamp is older than
    ``heartbeat_timeout_seconds`` (default: 120 = 2 minutes)

Note: ``stale`` ≠ ``dead``.  A stale heartbeat means the agent stopped
reporting, which might be a transient issue or a sign of a stuck agent.
Use SpawnRegistry.is_agent_alive() for a definitive liveness check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from clawteam.team.models import TaskItem, TaskStatus
from clawteam.team.tasks import TaskStore


DEFAULT_TASK_TIMEOUT = 600      # 10 minutes
DEFAULT_HEARTBEAT_STALE = 120   # 2 minutes


def _age_seconds(ts_iso: str) -> float:
    """Return elapsed seconds since *ts_iso* (ISO-8601). Returns inf on error."""
    try:
        ts = datetime.fromisoformat(ts_iso)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")


@dataclass
class StuckTask:
    """A task flagged as stuck."""

    task: TaskItem
    age_seconds: float
    reason: str      # "no_heartbeat" | "stale_heartbeat" | "timeout"


@dataclass
class StaleAgent:
    """An agent with a stale (or missing) heartbeat."""

    agent_name: str
    heartbeat_age_seconds: float    # inf = no heartbeat file at all
    current_task: str | None = None


@dataclass
class TimeoutReport:
    """Summary produced by TimeoutWatcher.check()."""

    team: str
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stuck_tasks: list[StuckTask] = field(default_factory=list)
    stale_agents: list[StaleAgent] = field(default_factory=list)
    task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT
    heartbeat_timeout_seconds: float = DEFAULT_HEARTBEAT_STALE

    @property
    def healthy(self) -> bool:
        return not self.stuck_tasks and not self.stale_agents

    def to_dict(self) -> dict[str, Any]:
        return {
            "team": self.team,
            "checkedAt": self.checked_at,
            "healthy": self.healthy,
            "taskTimeoutSeconds": self.task_timeout_seconds,
            "heartbeatTimeoutSeconds": self.heartbeat_timeout_seconds,
            "stuckTasks": [
                {
                    "id": s.task.id,
                    "subject": s.task.subject,
                    "owner": s.task.owner,
                    "lockedBy": s.task.locked_by,
                    "ageSeconds": round(s.age_seconds, 1),
                    "reason": s.reason,
                }
                for s in self.stuck_tasks
            ],
            "staleAgents": [
                {
                    "agent": a.agent_name,
                    "heartbeatAgeSeconds": round(a.heartbeat_age_seconds, 1) if a.heartbeat_age_seconds != float("inf") else None,
                    "currentTask": a.current_task,
                }
                for a in self.stale_agents
            ],
        }


class TimeoutWatcher:
    """Scans tasks and heartbeats to detect stuck/stale conditions.

    This class is intentionally *read-only* — it reports problems but
    does not auto-fix them.  Use AutoRestart or TaskStore.release_stale_locks()
    to act on the report.
    """

    def __init__(
        self,
        team_name: str,
        task_timeout_seconds: float = DEFAULT_TASK_TIMEOUT,
        heartbeat_timeout_seconds: float = DEFAULT_HEARTBEAT_STALE,
    ):
        self.team_name = team_name
        self.task_timeout_seconds = task_timeout_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_stuck_tasks(self) -> list[StuckTask]:
        """Return tasks that have been in_progress longer than the timeout."""
        store = TaskStore(self.team_name)
        tasks = store.list_tasks(status=TaskStatus.in_progress)

        # Try to load heartbeat info for context
        try:
            from clawteam.team.heartbeat import HeartbeatManager
            hb_mgr = HeartbeatManager(self.team_name)
            heartbeats = {r.agent: r for r in hb_mgr.list_all()}
        except Exception:
            heartbeats = {}

        stuck: list[StuckTask] = []
        for task in tasks:
            # Prefer lockedAt, fall back to startedAt, then updatedAt
            ref_ts = task.locked_at or task.started_at or task.updated_at
            if not ref_ts:
                continue

            age = _age_seconds(ref_ts)
            if age < self.task_timeout_seconds:
                continue

            # Determine reason
            owner = task.owner or task.locked_by
            hb = heartbeats.get(owner) if owner else None
            if hb is None:
                reason = "no_heartbeat"
            elif _age_seconds(hb.timestamp) > self.heartbeat_timeout_seconds:
                reason = "stale_heartbeat"
            else:
                reason = "timeout"

            stuck.append(StuckTask(task=task, age_seconds=age, reason=reason))

        return stuck

    def find_stale_agents(self) -> list[StaleAgent]:
        """Return agents whose heartbeat is missing or older than threshold.

        Only considers agents that currently own at least one in_progress task
        so that workers who finished and exited don't show as stale.
        """
        store = TaskStore(self.team_name)
        in_progress_tasks = store.list_tasks(status=TaskStatus.in_progress)

        # Collect agents with active work
        active_owners: dict[str, str | None] = {}   # agent_name -> current_task_id
        for t in in_progress_tasks:
            owner = t.owner or t.locked_by
            if owner:
                active_owners[owner] = t.id

        if not active_owners:
            return []

        try:
            from clawteam.team.heartbeat import HeartbeatManager
            hb_mgr = HeartbeatManager(self.team_name)
            heartbeats = {r.agent: r for r in hb_mgr.list_all()}
        except Exception:
            heartbeats = {}

        stale: list[StaleAgent] = []
        for agent_name, task_id in active_owners.items():
            hb = heartbeats.get(agent_name)
            if hb is None:
                stale.append(StaleAgent(
                    agent_name=agent_name,
                    heartbeat_age_seconds=float("inf"),
                    current_task=task_id,
                ))
            else:
                age = _age_seconds(hb.timestamp)
                if age > self.heartbeat_timeout_seconds:
                    stale.append(StaleAgent(
                        agent_name=agent_name,
                        heartbeat_age_seconds=age,
                        current_task=hb.current_task or task_id,
                    ))

        return stale

    def check(self) -> TimeoutReport:
        """Run all checks and return a consolidated TimeoutReport."""
        report = TimeoutReport(
            team=self.team_name,
            task_timeout_seconds=self.task_timeout_seconds,
            heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
        )
        report.stuck_tasks = self.find_stuck_tasks()
        report.stale_agents = self.find_stale_agents()
        return report

    # ------------------------------------------------------------------
    # Recovery helpers (act on findings)
    # ------------------------------------------------------------------

    def reset_stuck_tasks(self) -> list[str]:
        """Reset all stuck tasks back to pending. Returns list of task IDs reset."""
        store = TaskStore(self.team_name)
        stuck = self.find_stuck_tasks()
        reset_ids: list[str] = []
        for s in stuck:
            store.update(s.task.id, status=TaskStatus.pending)
            reset_ids.append(s.task.id)
        return reset_ids
