"""HeartbeatManager - agents report liveness and status every 30s.

Each agent writes a heartbeat file to:
  ~/.clawteam/teams/{team}/heartbeats/{agent}.json

A heartbeat records the agent's current status, optional current task,
and a timestamp. Any observer can read heartbeats to determine whether
agents are still alive and what they are working on.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from clawteam.team.models import get_data_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _heartbeats_dir(team_name: str) -> Path:
    d = get_data_dir() / "teams" / team_name / "heartbeats"
    d.mkdir(parents=True, exist_ok=True)
    return d


class HeartbeatRecord(BaseModel):
    """A single heartbeat record for one agent."""

    model_config = {"populate_by_name": True}

    agent: str
    team: str
    timestamp: str = Field(default_factory=_now_iso)
    status: str = "working"        # working | idle | finishing
    current_task: str | None = Field(default=None, alias="currentTask")
    progress_percent: int | None = Field(default=None, alias="progressPercent")
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatManager:
    """Manages heartbeat records for a team.

    Usage (from within an agent):
        hb = HeartbeatManager("my-team")
        hb.send("alice", status="working", current_task="abc123")

    The record is written atomically so concurrent readers never see
    a partial write.
    """

    def __init__(self, team_name: str):
        self.team_name = team_name

    # ------------------------------------------------------------------
    # Write side (called by the agent itself)
    # ------------------------------------------------------------------

    def send(
        self,
        agent_name: str,
        status: str = "working",
        current_task: str | None = None,
        progress_percent: int | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HeartbeatRecord:
        """Write (or overwrite) the heartbeat file for *agent_name*.

        This is safe to call repeatedly — each call just updates the
        single heartbeat file in place.
        """
        record = HeartbeatRecord(
            agent=agent_name,
            team=self.team_name,
            status=status,
            current_task=current_task,
            progress_percent=progress_percent,
            message=message,
            metadata=metadata or {},
        )
        self._save(record)
        return record

    # ------------------------------------------------------------------
    # Read side (called by watchers / health monitors)
    # ------------------------------------------------------------------

    def get(self, agent_name: str) -> HeartbeatRecord | None:
        """Return the latest heartbeat for *agent_name*, or None."""
        path = _heartbeats_dir(self.team_name) / f"{agent_name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return HeartbeatRecord.model_validate(data)
        except Exception:
            return None

    def list_all(self) -> list[HeartbeatRecord]:
        """Return heartbeats for all agents on the team."""
        records: list[HeartbeatRecord] = []
        d = _heartbeats_dir(self.team_name)
        for path in sorted(d.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(HeartbeatRecord.model_validate(data))
            except Exception:
                continue
        return records

    def is_stale(self, record: HeartbeatRecord, max_age_seconds: float = 60.0) -> bool:
        """Return True if *record* has not been updated within *max_age_seconds*."""
        try:
            ts = datetime.fromisoformat(record.timestamp)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return age > max_age_seconds
        except Exception:
            return True

    def age_seconds(self, record: HeartbeatRecord) -> float:
        """Return how many seconds ago the heartbeat was written."""
        try:
            ts = datetime.fromisoformat(record.timestamp)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return float("inf")

    def delete(self, agent_name: str) -> None:
        """Remove the heartbeat file for *agent_name* (e.g. on clean exit)."""
        path = _heartbeats_dir(self.team_name) / f"{agent_name}.json"
        path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save(self, record: HeartbeatRecord) -> None:
        d = _heartbeats_dir(self.team_name)
        target = d / f"{record.agent}.json"
        fd, tmp_name = tempfile.mkstemp(dir=d, prefix=f"{record.agent}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(record.model_dump_json(indent=2, by_alias=True))
            Path(tmp_name).replace(target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
