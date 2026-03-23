"""Auto-execution plugin for ClawTeam - triggers agents to auto-execute tasks after spawn."""

import os
import time
from typing import Optional

from clawteam.team.mailbox import MailboxManager
from clawteam.team.manager import TeamManager
from clawteam.team.tasks import TaskStore


def auto_trigger_agent_tasks(team_name: str, agent_name: str, agent_id: str, agent_type: str):
    """Automatically trigger agent to execute its assigned tasks after spawn.
    
    This function should be called immediately after an agent is spawned.
    It sends an initial trigger message to the agent's inbox to start task execution.
    """
    mailbox = MailboxManager(team_name)
    task_store = TaskStore(team_name)
    
    # Get tasks assigned to this agent
    tasks = task_store.list_tasks(owner=agent_name)
    pending_tasks = [t for t in tasks if t.status.value == "pending"]
    
    if not pending_tasks:
        # If no specific tasks assigned, get all pending tasks
        all_pending = task_store.list_tasks()
        pending_tasks = [t for t in all_pending if t.status.value == "pending" and not t.owner]
    
    if pending_tasks:
        # Build trigger message with task details
        task_list = "\n".join([
            f"- {t.subject} (ID: {t.id})" + (f": {t.description}" if t.description else "")
            for t in pending_tasks[:5]  # Limit to first 5 tasks
        ])
        
        trigger_message = f"""🚀 AUTO-EXECUTION TRIGGER

You have been spawned and assigned tasks. Start executing immediately.

Your Role: {agent_type}
Team: {team_name}

Assigned Tasks:
{task_list}

ACTION REQUIRED:
1. Check your tasks: `clawteam task list {team_name} --owner {agent_name}`
2. Start the first task: `clawteam task update {team_name} <task-id> --status in_progress`
3. Execute the task and report results via inbox
4. Mark completed: `clawteam task update {team_name} <task-id> --status completed`

Begin execution now. Do not wait for additional instructions.
"""
        
        # Send trigger message to agent's inbox
        mailbox.send(
            from_agent="system",
            to=agent_name,
            msg_type="auto_trigger",
            content=trigger_message,
        )
        
        # Also auto-claim first pending task if unowned
        for task in pending_tasks:
            if not task.owner:
                try:
                    task_store.update(
                        task.id,
                        status="in_progress",
                        owner=agent_name,
                        caller="system",
                        force=True,
                    )
                    break  # Only claim one task
                except Exception:
                    pass  # Task may already be claimed


def enable_auto_execution():
    """Enable auto-execution globally via environment variable."""
    os.environ["CLAWTEAM_AUTO_EXECUTE"] = "1"


def is_auto_execution_enabled() -> bool:
    """Check if auto-execution is enabled."""
    return os.environ.get("CLAWTEAM_AUTO_EXECUTE", "0") == "1"
