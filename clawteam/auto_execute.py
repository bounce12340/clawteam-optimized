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


# ============================================================================
# Auto-termination functionality
# ============================================================================

class AgentAutoTerminator:
    """Monitors agent activity and auto-terminates when idle or tasks completed."""
    
    def __init__(self, team_name: str, agent_name: str, 
                 idle_timeout_minutes: int = 30,
                 check_interval_seconds: int = 60):
        self.team_name = team_name
        self.agent_name = agent_name
        self.idle_timeout = timedelta(minutes=idle_timeout_minutes)
        self.check_interval = check_interval_seconds
        self.last_activity = datetime.now()
        self._stop_event = threading.Event()
        self._thread = None
    
    def start_monitoring(self):
        """Start the auto-termination monitoring thread."""
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(f"Auto-terminator started for {self.agent_name} in {self.team_name}")
    
    def stop_monitoring(self):
        """Stop the monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
    
    def record_activity(self):
        """Call this whenever the agent performs activity."""
        self.last_activity = datetime.now()
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        task_store = TaskStore(self.team_name)
        
        while not self._stop_event.is_set():
            time.sleep(self.check_interval)
            
            # Check if all tasks are completed
            tasks = task_store.list_tasks(owner=self.agent_name)
            pending_tasks = [t for t in tasks if t.status.value in ("pending", "in_progress")]
            
            if not pending_tasks:
                # All tasks completed - exit
                logger.info(f"All tasks completed for {self.agent_name}, auto-terminating")
                self._exit_agent("All tasks completed")
                return
            
            # Check idle timeout
            idle_duration = datetime.now() - self.last_activity
            if idle_duration > self.idle_timeout:
                logger.info(f"Agent {self.agent_name} idle for {idle_duration}, auto-terminating")
                self._exit_agent(f"Idle timeout ({idle_duration})")
                return
    
    def _exit_agent(self, reason: str):
        """Exit the agent process gracefully."""
        try:
            # Send completion message to leader
            mailbox = MailboxManager(self.team_name)
            mailbox.send(
                from_agent=self.agent_name,
                to="strategy-lead",
                msg_type=MessageType.task_completed,
                content=f"Agent {self.agent_name} auto-terminated: {reason}",
            )
        except Exception as e:
            logger.warning(f"Failed to send termination message: {e}")
        
        # Exit the process
        logger.info(f"Exiting agent {self.agent_name}: {reason}")
        sys.exit(0)


def start_auto_termination(team_name: str, agent_name: str,
                           idle_timeout_minutes: int = 30) -> AgentAutoTerminator:
    """Start auto-termination monitoring for this agent.
    
    Call this when agent starts up. The agent will automatically exit when:
    - All assigned tasks are completed, OR
    - Agent has been idle for the specified timeout
    
    Args:
        team_name: Name of the team
        agent_name: Name of this agent
        idle_timeout_minutes: How long before auto-terminating when idle
    
    Returns:
        AgentAutoTerminator instance (call record_activity() on it periodically)
    """
    terminator = AgentAutoTerminator(
        team_name=team_name,
        agent_name=agent_name,
        idle_timeout_minutes=idle_timeout_minutes
    )
    terminator.start_monitoring()
    return terminator
