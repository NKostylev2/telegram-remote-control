"""Communication protocol models and security utilities for Controller and Agent.

Defines the v2 protocol specification for bidirectional communication between
Android Controller and Windows Agent:
- Protocol versioning and limits.
- HMAC-SHA256 request signing and verification with replay protection.
- Heartbeat registration and state monitoring.
- Task assignment, status reporting, and result delivery.
- Desktop screenshot negotiation.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from remote_control.common.models import TaskStatus

# Protocol specifications and limits
PROTOCOL_VERSION = "2.0"
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
MIN_POLL_INTERVAL_SECONDS = 0.1
MAX_POLL_INTERVAL_SECONDS = 10.0
HEARTBEAT_TIMEOUT_SECONDS = 30.0
MAX_TIMESTAMP_DRIFT_SECONDS = 60.0
MAX_FILE_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB limit (Telegram send document limit)
MAX_FILE_UPLOAD_BYTES = 20 * 1024 * 1024    # 20 MB limit (Telegram bot download limit)
MAX_PAYLOAD_BYTES = 60 * 1024 * 1024        # 60 MB limit for server payload



class AgentState(StrEnum):
    """Availability state of Windows Agent tracked by Controller."""

    OFFLINE = "offline"
    ONLINE_IDLE = "online_idle"
    ONLINE_BUSY = "online_busy"
    UNREACHABLE = "unreachable"


# -----------------------------------------------------------------------------
# Cryptographic Authentication & Replay Protection
# -----------------------------------------------------------------------------


def compute_signature(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    body: bytes = b"",
) -> str:
    """Compute HMAC-SHA256 signature over request metadata and body.

    Format of signed payload:
    `{timestamp}\n{method.upper()}\n{path}\n{sha256(body)}`
    """
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{timestamp}\n{method.upper()}\n{path}\n{body_hash}".encode()
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    signature: str,
    timestamp_str: str,
    method: str,
    path: str,
    body: bytes = b"",
    max_drift_seconds: float = MAX_TIMESTAMP_DRIFT_SECONDS,
) -> tuple[bool, str]:
    """Verify HMAC-SHA256 signature and check timestamp against replay attacks.

    Returns:
        (is_valid, error_reason)
    """
    if not secret:
        return False, "Agent authentication secret is not configured"
    if not signature:
        return False, "Missing X-Signature header"
    if not timestamp_str:
        return False, "Missing X-Timestamp header"

    try:
        req_timestamp = float(timestamp_str)
    except (ValueError, TypeError):
        return False, "Invalid timestamp format (expected float/int epoch seconds)"

    now = time.time()
    drift = abs(now - req_timestamp)
    if drift > max_drift_seconds:
        return False, f"Timestamp drift exceeds limit ({drift:.1f}s > {max_drift_seconds:.1f}s)"

    expected_sig = compute_signature(secret, timestamp_str, method, path, body)
    if not hmac.compare_digest(signature, expected_sig):
        return False, "Invalid HMAC signature"

    return True, ""


def build_auth_headers(
    agent_id: str,
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
) -> dict[str, str]:
    """Generate authenticated HTTP request headers including HMAC signature."""
    now_ts = str(time.time())
    sig = compute_signature(secret, now_ts, method, path, body)
    return {
        "X-Agent-ID": agent_id,
        "X-Timestamp": now_ts,
        "X-Signature": sig,
        "X-Protocol-Version": PROTOCOL_VERSION,
    }


# -----------------------------------------------------------------------------
# Protocol Data Models
# -----------------------------------------------------------------------------


class HeartbeatRequest(BaseModel):
    """Heartbeat and capability broadcast sent periodically by Windows Agent."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    agent_id: str
    hostname: str
    platform: str = "windows"
    cli_available: bool = False
    cli_version: str | None = None
    available_workspaces: dict[str, str] = Field(default_factory=dict)
    available_drives: list[str] = Field(default_factory=list)
    current_task_id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HeartbeatResponse(BaseModel):
    """Heartbeat acknowledgement sent by Android Controller."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    acknowledged: bool = True
    server_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_task_id: int | None = None
    cancel_task_ids: list[int] = Field(default_factory=list)
    screenshot_requested: bool = False
    browse_request_id: str | None = None
    browse_request_path: str | None = None
    file_download_request_id: str | None = None
    file_download_path: str | None = None
    file_upload_request_id: str | None = None


class AgentTaskAssignment(BaseModel):
    """Task execution specification dispatched from Controller to Windows Agent."""

    model_config = ConfigDict(frozen=True)

    task_id: int
    operation_id: str
    workspace_alias: str
    workspace_path: str | None = None
    prompt: str
    effort: str = "low"
    timeout_seconds: int = 300
    auto_approve_permissions: bool = True
    conversation_id: str | None = None
    continue_session: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PollTasksRequest(BaseModel):
    """Queue query sent by Windows Agent with short polling interval."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    agent_id: str
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    max_tasks: int = 1


class PollTasksResponse(BaseModel):
    """Queue query response containing zero or more assigned tasks."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    tasks: list[AgentTaskAssignment] = Field(default_factory=list)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    browse_request_id: str | None = None
    browse_request_path: str | None = None
    file_download_request_id: str | None = None
    file_download_path: str | None = None
    file_upload_request_id: str | None = None


class TaskStatusUpdateRequest(BaseModel):
    """Progress or state change notification sent by Windows Agent."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    agent_id: str
    task_id: int
    status: TaskStatus
    message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskStatusUpdateResponse(BaseModel):
    """Acknowledgement for task status update."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    acknowledged: bool = True
    cancel_requested: bool = False


class TaskResultSubmission(BaseModel):
    """Final task execution result payload sent by Windows Agent."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    agent_id: str
    task_id: int
    status: TaskStatus
    exit_code: int = 0
    response: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    soft_denied_tools: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conversation_id: str | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskResultSubmissionResponse(BaseModel):
    """Acknowledgement of task completion."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    acknowledged: bool = True
    task_id: int


class ScreenshotMetadata(BaseModel):
    """Metadata accompanying desktop screenshot capture."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    agent_id: str
    width: int
    height: int
    format: str = "jpeg"
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FileItem(BaseModel):
    """Representation of a filesystem file in directory listing."""

    model_config = ConfigDict(frozen=True)

    name: str
    size_bytes: int
    modified_at: float | None = None


class DirectoryBrowseRequest(BaseModel):
    """Directory inspection instruction sent by Controller."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    path: str


class DirectoryBrowseResponse(BaseModel):
    """Directory listing result submitted by Windows Agent."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str = PROTOCOL_VERSION
    request_id: str
    path: str
    subdirectories: list[str] = Field(default_factory=list)
    files: list[FileItem] = Field(default_factory=list)
    error: str | None = None


class FileDownloadInstruction(BaseModel):
    """Instruction for Windows Agent to upload a local file to Controller."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    path: str


class FileUploadInstruction(BaseModel):
    """Metadata for a pending file download from Controller to Windows Agent."""

    model_config = ConfigDict(frozen=True)

    transfer_id: str
    target_dir: str
    filename: str
    conflict_mode: str = "overwrite"  # "overwrite" | "suffix"
    file_size: int = 0


class FileUploadAck(BaseModel):
    """Acknowledgement sent by Windows Agent after writing an uploaded file."""

    model_config = ConfigDict(frozen=True)

    transfer_id: str
    success: bool
    saved_path: str = ""
    bytes_written: int = 0
    error: str | None = None



class AgentStatusInfo(BaseModel):
    """Snapshot of connected agent status maintained in memory by Controller."""

    agent_id: str
    hostname: str
    platform: str
    cli_available: bool
    cli_version: str | None = None
    available_workspaces: dict[str, str] = Field(default_factory=dict)
    available_drives: list[str] = Field(default_factory=list)
    last_heartbeat: datetime
    is_online: bool
    current_task_id: int | None = None
