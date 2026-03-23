"""ProgressTracker - task-level progress reporting.

Agents update their task progress (0-100 %) via ``ProgressTracker.update()``.
Progress is stored inside the task's ``metadata`` dict so it is visible
anywhere the TaskItem is read (board, CLI, waiter, etc.).

Metadata keys used
------------------
``progress_percent``      int   0-100
``progress_message``      str   Human-readable status line
``progress_updated_at``   str   ISO-8601 timestamp of last update

Convenience
-----------
``render_bar(percent)`` returns a unicode progress bar string suitable
for terminal display, e.g.::

    [████████░░░░░░░░░░░░]  40%
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProgressTracker:
    """Updates and retrieves progress metadata on tasks."""

    def __init__(self, team_name: str):
        self.team_name = team_name

    def update(
        self,
        task_id: str,
        percent: int,
        message: str = "",
        caller: str = "",
    ) -> "Any | None":
        """Set progress on *task_id*.

        Parameters
        ----------
        task_id:
            The task to update.
        percent:
            Completion percentage, 0-100.
        message:
            Optional human-readable status description.
        caller:
            Agent name performing the update (for audit trail).

        Returns
        -------
        Updated ``TaskItem`` or None if not found.
        """
        percent = max(0, min(100, percent))
        from clawteam.team.tasks import TaskStore
        store = TaskStore(self.team_name)
        metadata: dict[str, Any] = {
            "progress_percent": percent,
            "progress_updated_at": _now_iso(),
        }
        if message:
            metadata["progress_message"] = message
        return store.update(task_id, metadata=metadata, caller=caller)

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Return the progress fields from *task_id*'s metadata, or None."""
        from clawteam.team.tasks import TaskStore
        task = TaskStore(self.team_name).get(task_id)
        if not task:
            return None
        meta = task.metadata
        return {
            "task_id": task_id,
            "subject": task.subject,
            "status": task.status.value,
            "owner": task.owner,
            "percent": meta.get("progress_percent"),
            "message": meta.get("progress_message", ""),
            "updated_at": meta.get("progress_updated_at", ""),
        }

    def list_progress(self, owner: str | None = None) -> list[dict[str, Any]]:
        """Return progress dicts for all tasks, optionally filtered by *owner*."""
        from clawteam.team.tasks import TaskStore
        store = TaskStore(self.team_name)
        tasks = store.list_tasks(owner=owner)
        result = []
        for task in tasks:
            result.append({
                "task_id": task.id,
                "subject": task.subject,
                "status": task.status.value,
                "owner": task.owner,
                "percent": task.metadata.get("progress_percent"),
                "message": task.metadata.get("progress_message", ""),
                "updated_at": task.metadata.get("progress_updated_at", ""),
            })
        return result


# ---------------------------------------------------------------------------
# Terminal rendering helpers
# ---------------------------------------------------------------------------

def render_bar(percent: int | None, width: int = 20) -> str:
    """Return a unicode block progress bar.

    Example::

        render_bar(40)  →  "[████████░░░░░░░░░░░░]  40%"
        render_bar(None) →  "[────────────────────]   ?"
    """
    if percent is None:
        bar = "─" * width
        return f"[{bar}]  ?%"
    filled = round(width * max(0, min(100, percent)) / 100)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percent:3d}%"


def render_progress_table(progress_list: list[dict]) -> str:
    """Return a plain-text summary table for a list of progress dicts."""
    lines = []
    for p in progress_list:
        bar = render_bar(p.get("percent"))
        task_id = p.get("task_id", "?")[:8]
        subject = (p.get("subject") or "")[:35]
        owner = p.get("owner") or "-"
        status = p.get("status") or "-"
        msg = p.get("message") or ""
        line = f"  {task_id}  {bar}  [{status}] {owner:12s}  {subject}"
        if msg:
            line += f"\n             {msg}"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no tasks)"
