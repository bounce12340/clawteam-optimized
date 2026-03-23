"""Auto-execution plugin for ClawTeam - triggers agents to auto-execute tasks after spawn."""

import logging
from pathlib import Path

from clawteam.team.mailbox import MailboxManager
from clawteam.team.models import MessageType, TaskStatus
from clawteam.team.tasks import TaskStore

logger = logging.getLogger(__name__)


def auto_trigger_agent_tasks(team_name: str, agent_name: str, agent_id: str, agent_type: str):
    """Automatically trigger agent to execute its assigned tasks after spawn.

    - Auto-claims owned pending tasks as in_progress so the task status
      reflects reality immediately (before the agent process has a chance to
      call task update itself).
    - Sends a start trigger message to the agent's inbox so that agents which
      poll their inbox on startup get an immediate kick.
    """
    task_store = TaskStore(team_name)
    mailbox = MailboxManager(team_name)

    # Collect tasks assigned to this agent that are still pending
    tasks = task_store.list_tasks(owner=agent_name)
    pending_tasks = [t for t in tasks if t.status == TaskStatus.pending]

    logger.info(
        "auto_trigger: team=%s agent=%s pending_tasks=%d",
        team_name, agent_name, len(pending_tasks),
    )

    # Auto-claim all owned pending tasks as in_progress
    claimed = []
    for task in pending_tasks:
        try:
            task_store.update(
                task.id,
                status=TaskStatus.in_progress,
                caller=agent_name,
            )
            claimed.append(task)
            logger.info("auto_trigger: claimed task %s (%s)", task.id, task.subject)
        except Exception as exc:
            logger.warning("auto_trigger: could not claim task %s: %s", task.id, exc)

    # Build a concise start message for the agent's inbox
    if claimed:
        task_lines = "\n".join(
            f"- {t.subject} (ID: {t.id})" for t in claimed
        )
        content = (
            f"Team '{team_name}' has launched. Your tasks are now IN PROGRESS — "
            f"begin working on them immediately.\n\n"
            f"Your tasks:\n{task_lines}\n\n"
            f"Run `clawteam task list {team_name} --owner {agent_name}` to confirm, "
            f"then complete each task and update its status to completed."
        )
    else:
        content = (
            f"Team '{team_name}' has launched. "
            f"Run `clawteam task list {team_name} --owner {agent_name}` to see your tasks "
            f"and begin working immediately."
        )

    try:
        mailbox.send(
            from_agent="system",
            to=agent_name,
            msg_type=MessageType.message,
            content=content,
        )
        logger.info("auto_trigger: sent start message to %s", agent_name)
    except Exception as exc:
        logger.warning("auto_trigger: could not send message to %s: %s", agent_name, exc)


def is_auto_execution_enabled() -> bool:
    """Check if auto-execution is enabled.

    Auto-execution is enabled when:
    - CLAWTEAM_AUTO_EXECUTE env var is set to '1', OR
    - the .clawteam/auto_execute flag file exists
    """
    import os

    if os.environ.get("CLAWTEAM_AUTO_EXECUTE", "0") == "1":
        return True
    from clawteam.team.models import get_data_dir
    flag = get_data_dir() / "auto_execute"
    return flag.exists()


def enable_auto_execution():
    """Persist auto-execution flag so it survives process restarts."""
    from clawteam.team.models import get_data_dir
    flag = get_data_dir() / "auto_execute"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


def disable_auto_execution():
    """Remove the persistent auto-execution flag."""
    from clawteam.team.models import get_data_dir
    flag = get_data_dir() / "auto_execute"
    flag.unlink(missing_ok=True)
