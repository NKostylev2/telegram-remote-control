"""Persistent SQLite task store and migration engine."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from remote_control.common.models import TaskResult, TaskStatus
from remote_control.controller.confirmation import PendingConfirmation

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_MAX_RESULT_FILE_BYTES = 10 * 1024 * 1024  # 10 MB limit


class TaskStoreError(Exception):
    """Base exception for task store operations."""


class DuplicateOperationError(TaskStoreError):
    """Raised when an operation with the same confirmation ID is already enqueued."""


class TaskNotFoundError(TaskStoreError):
    """Raised when a task with specified ID is not found."""


class TaskInvalidStateError(TaskStoreError):
    """Raised when an invalid state transition is attempted."""


@dataclass(frozen=True)
class TaskRecord:
    """Immutable representation of a task record persisted in SQLite."""

    id: int
    operation_id: str
    user_id: int
    chat_id: int
    workspace_alias: str
    workspace_path: Path | None
    prompt: str
    interpretation: str | None
    effort: str
    status: TaskStatus
    created_at: datetime
    confirmed_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    conversation_id: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    result_summary: str | None = None
    result_file_path: Path | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    soft_denied_tools: list[str] = field(default_factory=list)


def _iso_to_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def _datetime_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


class TaskStore:
    """Thread-safe and async-compatible SQLite task store."""

    def __init__(self, database_path: Path, results_dir: Path) -> None:
        self.database_path = database_path.resolve()
        self.results_dir = results_dir.resolve()
        self._lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Create and configure a SQLite connection."""
        conn = sqlite3.connect(
            str(self.database_path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def initialize_sync(self) -> None:
        """Synchronously initialize directory, database schema and migrations."""
        with self._lock:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.results_dir.mkdir(parents=True, exist_ok=True)

            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA user_version")
                current_version = cursor.fetchone()[0]

                if current_version == 0:
                    logger.info("Initializing task store schema (v%d)...", SCHEMA_VERSION)
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS tasks (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            operation_id TEXT UNIQUE NOT NULL,
                            user_id INTEGER NOT NULL,
                            chat_id INTEGER NOT NULL,
                            workspace_alias TEXT NOT NULL,
                            workspace_path TEXT,
                            prompt TEXT NOT NULL,
                            interpretation TEXT,
                            effort TEXT NOT NULL DEFAULT 'low',
                            status TEXT NOT NULL DEFAULT 'queued',
                            created_at TEXT NOT NULL,
                            confirmed_at TEXT NOT NULL,
                            queued_at TEXT NOT NULL,
                            started_at TEXT,
                            completed_at TEXT,
                            conversation_id TEXT,
                            exit_code INTEGER,
                            duration_seconds REAL,
                            result_summary TEXT,
                            result_file_path TEXT,
                            error_message TEXT,
                            warnings TEXT,
                            soft_denied_tools TEXT
                        )
                        """
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id, id DESC)"
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_tasks_operation_id ON tasks(operation_id)"
                    )
                    cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()
                elif current_version < SCHEMA_VERSION:
                    logger.info(
                        "Migrating schema from v%d to v%d...", current_version, SCHEMA_VERSION
                    )
                    # Incremental migrations would be placed here
                    cursor.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()

    async def initialize(self) -> None:
        """Asynchronously initialize database schema and directories."""
        await asyncio.to_thread(self.initialize_sync)

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        """Convert a sqlite3.Row to an immutable TaskRecord."""
        raw_warnings = row["warnings"]
        warnings = json.loads(raw_warnings) if raw_warnings else []

        raw_soft_denied = row["soft_denied_tools"]
        soft_denied_tools = json.loads(raw_soft_denied) if raw_soft_denied else []

        ws_path_str = row["workspace_path"]
        workspace_path = Path(ws_path_str) if ws_path_str else None

        result_path_str = row["result_file_path"]
        result_file_path = Path(result_path_str) if result_path_str else None

        created_at = _iso_to_datetime(row["created_at"]) or datetime.now(UTC)
        confirmed_at = _iso_to_datetime(row["confirmed_at"]) or datetime.now(UTC)
        queued_at = _iso_to_datetime(row["queued_at"]) or datetime.now(UTC)

        return TaskRecord(
            id=row["id"],
            operation_id=row["operation_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            workspace_alias=row["workspace_alias"],
            workspace_path=workspace_path,
            prompt=row["prompt"],
            interpretation=row["interpretation"],
            effort=row["effort"] or "low",
            status=TaskStatus(row["status"]),
            created_at=created_at,
            confirmed_at=confirmed_at,
            queued_at=queued_at,
            started_at=_iso_to_datetime(row["started_at"]),
            completed_at=_iso_to_datetime(row["completed_at"]),
            conversation_id=row["conversation_id"],
            exit_code=row["exit_code"],
            duration_seconds=row["duration_seconds"],
            result_summary=row["result_summary"],
            result_file_path=result_file_path,
            error_message=row["error_message"],
            warnings=warnings,
            soft_denied_tools=soft_denied_tools,
        )

    def enqueue_task_sync(self, op: PendingConfirmation) -> TaskRecord:
        """Synchronously enqueue a confirmed task into SQLite."""
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            created_iso = datetime.fromtimestamp(op.created_at, tz=UTC).isoformat()

            with self._get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        INSERT INTO tasks (
                            operation_id, user_id, chat_id, workspace_alias, workspace_path,
                            prompt, interpretation, effort, status, created_at, confirmed_at,
                            queued_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            op.operation_id,
                            op.user_id,
                            op.chat_id,
                            op.workspace_alias,
                            str(op.workspace_path) if op.workspace_path else None,
                            op.prompt,
                            op.interpretation,
                            op.effort,
                            TaskStatus.QUEUED.value,
                            created_iso,
                            now_iso,
                            now_iso,
                        ),
                    )
                    conn.commit()
                    task_id = cursor.lastrowid
                except sqlite3.IntegrityError as e:
                    if "UNIQUE constraint failed: tasks.operation_id" in str(e):
                        raise DuplicateOperationError(
                            f"Operation {op.operation_id} is already enqueued in database."
                        ) from e
                    raise TaskStoreError(f"Database error enqueuing task: {e}") from e

                cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                row = cursor.fetchone()
                if not row:
                    raise TaskStoreError(f"Failed to fetch newly created task #{task_id}")
                return self._row_to_record(row)

    async def enqueue_task(self, op: PendingConfirmation) -> TaskRecord:
        """Asynchronously enqueue a confirmed task into SQLite."""
        return await asyncio.to_thread(self.enqueue_task_sync, op)

    def get_task_sync(self, task_id: int) -> TaskRecord | None:
        """Synchronously retrieve a task record by ID."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                row = cursor.fetchone()
                return self._row_to_record(row) if row else None

    async def get_task(self, task_id: int) -> TaskRecord | None:
        """Asynchronously retrieve a task record by ID."""
        return await asyncio.to_thread(self.get_task_sync, task_id)

    def get_next_queued_task_sync(self) -> TaskRecord | None:
        """Synchronously select the oldest task currently in QUEUED status."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY id ASC LIMIT 1",
                    (TaskStatus.QUEUED.value,),
                )
                row = cursor.fetchone()
                return self._row_to_record(row) if row else None

    async def get_next_queued_task(self) -> TaskRecord | None:
        """Asynchronously select the oldest task currently in QUEUED status."""
        return await asyncio.to_thread(self.get_next_queued_task_sync)

    def mark_task_running_sync(self, task_id: int) -> bool:
        """Atomically transition a task from QUEUED to RUNNING."""
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE tasks
                    SET status = ?, started_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        TaskStatus.RUNNING.value,
                        now_iso,
                        task_id,
                        TaskStatus.QUEUED.value,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0

    async def mark_task_running(self, task_id: int) -> bool:
        """Asynchronously transition a task from QUEUED to RUNNING."""
        return await asyncio.to_thread(self.mark_task_running_sync, task_id)

    def mark_task_finished_sync(
        self,
        task_id: int,
        result: TaskResult,
        result_file_path: Path | None = None,
    ) -> bool:
        """Atomically record task completion and save terminal status."""
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            warnings_json = json.dumps(result.warnings, ensure_ascii=False)
            soft_denied_json = json.dumps(result.soft_denied_tools, ensure_ascii=False)

            # Store short summary directly in DB (up to 3000 chars)
            summary = result.response[:3000] if result.response else ""

            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE tasks
                    SET status = ?, completed_at = ?, conversation_id = ?, exit_code = ?,
                        duration_seconds = ?, result_summary = ?, result_file_path = ?,
                        error_message = ?, warnings = ?, soft_denied_tools = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        result.status.value,
                        now_iso,
                        result.conversation_id,
                        result.exit_code,
                        result.duration_seconds,
                        summary,
                        str(result_file_path) if result_file_path else None,
                        result.error or None,
                        warnings_json,
                        soft_denied_json,
                        task_id,
                        TaskStatus.RUNNING.value,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0

    async def mark_task_finished(
        self,
        task_id: int,
        result: TaskResult,
        result_file_path: Path | None = None,
    ) -> bool:
        """Asynchronously record task completion and save terminal status."""
        return await asyncio.to_thread(
            self.mark_task_finished_sync, task_id, result, result_file_path
        )

    def cancel_queued_task_sync(
        self, task_id: int, user_id: int
    ) -> tuple[bool, str, TaskRecord | None]:
        """Cancel a queued task if owned by user_id and still in QUEUED status."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                row = cursor.fetchone()
                if not row:
                    return False, f"Задача #{task_id} не найдена.", None

                if row["user_id"] != user_id:
                    return False, f"Вы не являетесь владельцем задачи #{task_id}.", None

                current_status = TaskStatus(row["status"])
                if current_status != TaskStatus.QUEUED:
                    return (
                        False,
                        f"Задача #{task_id} находится в статусе {current_status.value}.",
                        self._row_to_record(row),
                    )

                now_iso = datetime.now(UTC).isoformat()
                cursor.execute(
                    """
                    UPDATE tasks
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        TaskStatus.CANCELED.value,
                        now_iso,
                        "Задача отменена пользователем из очереди.",
                        task_id,
                        TaskStatus.QUEUED.value,
                    ),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    # Race condition: status changed between select and update
                    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                    updated_row = cursor.fetchone()
                    return (
                        False,
                        "Статус задачи изменился во время отмены.",
                        self._row_to_record(updated_row),
                    )

                cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                return True, "", self._row_to_record(cursor.fetchone())

    async def cancel_queued_task(
        self, task_id: int, user_id: int
    ) -> tuple[bool, str, TaskRecord | None]:
        """Asynchronously cancel a queued task."""
        return await asyncio.to_thread(self.cancel_queued_task_sync, task_id, user_id)

    def mark_task_canceled_sync(
        self, task_id: int, reason: str = "Отменено пользователем"
    ) -> bool:
        """Mark a running task as CANCELED."""
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE tasks
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        TaskStatus.CANCELED.value,
                        now_iso,
                        reason,
                        task_id,
                        TaskStatus.RUNNING.value,
                        TaskStatus.QUEUED.value,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0

    async def mark_task_canceled(
        self, task_id: int, reason: str = "Отменено пользователем"
    ) -> bool:
        """Asynchronously mark a task as CANCELED."""
        return await asyncio.to_thread(self.mark_task_canceled_sync, task_id, reason)

    def list_user_tasks_sync(
        self,
        user_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """Synchronously list recent tasks for a user ordered newest first."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM tasks
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, offset),
                )
                return [self._row_to_record(r) for r in cursor.fetchall()]

    async def list_user_tasks(
        self,
        user_id: int,
        limit: int = 10,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """Asynchronously list recent tasks for a user."""
        return await asyncio.to_thread(self.list_user_tasks_sync, user_id, limit, offset)

    def count_user_tasks_sync(self, user_id: int) -> int:
        """Synchronously count total tasks belonging to a user."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM tasks WHERE user_id = ?",
                    (user_id,),
                )
                return int(cursor.fetchone()[0])

    async def count_user_tasks(self, user_id: int) -> int:
        """Asynchronously count total tasks belonging to a user."""
        return await asyncio.to_thread(self.count_user_tasks_sync, user_id)

    def get_queue_position_sync(self, task_id: int) -> int:
        """Calculate the 1-indexed queue position of a task in QUEUED status."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM tasks
                    WHERE status = ? AND id <= ?
                    """,
                    (TaskStatus.QUEUED.value, task_id),
                )
                return int(cursor.fetchone()[0])

    async def get_queue_position(self, task_id: int) -> int:
        """Asynchronously calculate the queue position of a task."""
        return await asyncio.to_thread(self.get_queue_position_sync, task_id)

    def recover_on_startup_sync(self) -> tuple[int, int]:
        """Synchronously recover tasks on application startup.

        - Converts orphan RUNNING tasks to INTERRUPTED.
        - Preserves QUEUED tasks for continued processing.

        Returns:
            (interrupted_count, queued_count)
        """
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE tasks
                    SET status = ?, completed_at = ?,
                        error_message = 'Исполнение прервано перезапуском приложения.'
                    WHERE status = ?
                    """,
                    (
                        TaskStatus.INTERRUPTED.value,
                        now_iso,
                        TaskStatus.RUNNING.value,
                    ),
                )
                interrupted_count = cursor.rowcount

                cursor.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?",
                    (TaskStatus.QUEUED.value,),
                )
                queued_count = int(cursor.fetchone()[0])
                conn.commit()

            if interrupted_count > 0:
                logger.warning(
                    "Recovered on startup: %d orphan RUNNING task(s) marked INTERRUPTED.",
                    interrupted_count,
                )
            if queued_count > 0:
                logger.info(
                    "Recovered on startup: %d QUEUED task(s) waiting for execution.",
                    queued_count,
                )
            return interrupted_count, queued_count

    async def recover_on_startup(self) -> tuple[int, int]:
        """Asynchronously recover tasks on application startup."""
        return await asyncio.to_thread(self.recover_on_startup_sync)

    def save_result_file(self, task_id: int, content: str) -> Path:
        """Safely save task output to a file in results_dir.

        Enforces safe filename, directory containment, and size limit.
        """
        self.results_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"task_{task_id}.txt"
        target_path = (self.results_dir / safe_name).resolve()

        # Boundary check: ensure path does not escape results_dir
        if not str(target_path).startswith(str(self.results_dir)):
            raise TaskStoreError(f"Invalid path traversal attempt for task file: {safe_name}")

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_RESULT_FILE_BYTES:
            encoded = (
                encoded[:_MAX_RESULT_FILE_BYTES]
                + b"\n\n[... Truncated due to size limit ...]"
            )

        target_path.write_bytes(encoded)
        return target_path

    def read_result_file(self, task_id: int) -> str | None:
        """Safely read task output file from results_dir.

        Enforces directory containment.
        """
        safe_name = f"task_{task_id}.txt"
        target_path = (self.results_dir / safe_name).resolve()
        if not str(target_path).startswith(str(self.results_dir)):
            return None
        if not target_path.is_file():
            return None
        return target_path.read_text(encoding="utf-8", errors="replace")
