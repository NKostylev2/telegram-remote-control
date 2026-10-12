"""Shared data models and enums for Controller and Windows Agent."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class HostState(StrEnum):
    """Host availability state."""

    OFFLINE = "offline"
    ONLINE_IDLE = "online_idle"
    ONLINE_BUSY = "online_busy"
    AGENT_UNREACHABLE = "agent_unreachable"
    WAKING_UP = "waking_up"


class TaskStatus(StrEnum):
    """Lifecycle status of a task."""

    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    ERROR = "error"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


class TaskRequest(BaseModel):
    """Task submission model."""

    model_config = ConfigDict(frozen=True)

    task_id: UUID = Field(default_factory=uuid4)
    workspace_alias: str
    prompt: str
    requested_by: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskResult(BaseModel):
    """Task execution result returned by Windows Agent."""

    model_config = ConfigDict(frozen=True)

    task_id: UUID | str | int = Field(default_factory=uuid4)
    status: TaskStatus
    exit_code: int = 0
    response: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    soft_denied_tools: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
