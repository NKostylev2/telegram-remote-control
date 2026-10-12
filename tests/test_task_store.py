"""Unit tests for SQLite TaskStore persistence, schema migrations, and recovery."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remote_control.common.models import TaskStatus
from remote_control.controller.confirmation import PendingConfirmation
from remote_control.controller.task_store import (
    DuplicateOperationError,
    QueueFullError,
    TaskRecord,
    TaskStore,
)


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    """Create an initialized TaskStore in a temporary directory."""
    db_path = tmp_path / "data" / "tasks.sqlite3"
    results_dir = tmp_path / "data" / "results"
    store = TaskStore(db_path, results_dir)
    store.initialize_sync()
    return store


def _make_pending_confirmation(
    op_id: str = "test-op-1",
    user_id: int = 111222,
    chat_id: int = 111222,
    prompt: str = "Test prompt",
    alias: str = "default",
    effort: str = "low",
) -> PendingConfirmation:
    now = datetime.now(UTC).timestamp()
    return PendingConfirmation(
        operation_id=op_id,
        user_id=user_id,
        chat_id=chat_id,
        workspace_alias=alias,
        prompt=prompt,
        created_at=now,
        expires_at=now + 120.0,
        effort=effort,
    )


class TestTaskStoreInitializationAndMigration:
    """Tests for database initialization, missing dirs, and schema versioning."""

    def test_initialization_creates_missing_directories(self, tmp_path: Path) -> None:
        """Req 20: Directory created if missing on initialization."""
        nested_dir = tmp_path / "deep" / "nested" / "path"
        db_path = nested_dir / "tasks.sqlite3"
        results_dir = nested_dir / "results"

        assert not nested_dir.exists()
        store = TaskStore(db_path, results_dir)
        store.initialize_sync()

        assert db_path.is_file()
        assert results_dir.is_dir()

    def test_schema_version_and_idempotent_init(self, task_store: TaskStore) -> None:
        """Req 21: Schema versioning via PRAGMA user_version and idempotent re-init."""
        with sqlite3.connect(task_store.database_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 2

        # Re-running initialization should not fail or corrupt existing data
        task_store.initialize_sync()

        with sqlite3.connect(task_store.database_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 2

    def test_unconfirmed_operations_not_in_database(self, task_store: TaskStore) -> None:
        """Req 2: Pending confirmations alone are NOT stored in SQLite."""
        # Database should be empty before any confirmation
        assert task_store.get_next_queued_task_sync() is None
        assert task_store.count_user_tasks_sync(111222) == 0


class TestTaskEnqueueAndRetrieval:
    """Tests for enqueuing, unique constraints, and reading tasks."""

    def test_enqueue_task_creates_record(self, task_store: TaskStore) -> None:
        """Req 1: Enqueue creates record in SQLite with status QUEUED."""
        op = _make_pending_confirmation(op_id="op-1", prompt="Echo hello")
        record = task_store.enqueue_task_sync(op)

        assert isinstance(record, TaskRecord)
        assert record.id == 1
        assert record.operation_id == "op-1"
        assert record.prompt == "Echo hello"
        assert record.status == TaskStatus.QUEUED
        assert record.effort == "low"
        assert record.user_id == 111222

        # Verify persistent read from DB
        fetched = task_store.get_task_sync(record.id)
        assert fetched is not None
        assert fetched.id == record.id
        assert fetched.status == TaskStatus.QUEUED

    def test_duplicate_operation_id_rejected(self, task_store: TaskStore) -> None:
        """Req 4: Protection against duplicate callback using operation_id."""
        op = _make_pending_confirmation(op_id="dup-op-1")
        task_store.enqueue_task_sync(op)

        # Second attempt with same operation_id must raise DuplicateOperationError
        with pytest.raises(DuplicateOperationError):
            task_store.enqueue_task_sync(op)

    def test_history_preserved_after_store_reopen(self, tmp_path: Path) -> None:
        """Req 8: Tasks persist across closing and reopening database."""
        db_path = tmp_path / "tasks.sqlite3"
        results_dir = tmp_path / "results"

        # Instance 1: write task
        store1 = TaskStore(db_path, results_dir)
        store1.initialize_sync()
        record = store1.enqueue_task_sync(_make_pending_confirmation(op_id="persist-1"))

        # Instance 2: reopen same file
        store2 = TaskStore(db_path, results_dir)
        store2.initialize_sync()
        fetched = store2.get_task_sync(record.id)

        assert fetched is not None
        assert fetched.operation_id == "persist-1"
        assert fetched.status == TaskStatus.QUEUED


class TestStartupRecovery:
    """Tests for application recovery after restart."""

    def test_startup_recovery_preserves_queued_and_interrupts_running(
        self, task_store: TaskStore
    ) -> None:
        """Req 9 & 10: Preserves QUEUED tasks, converts orphan RUNNING to INTERRUPTED."""
        # 1. Enqueue task 1 (stays queued)
        op1 = _make_pending_confirmation(op_id="op-q1")
        t1 = task_store.enqueue_task_sync(op1)

        # 2. Enqueue task 2 and mark it running
        op2 = _make_pending_confirmation(op_id="op-r2")
        t2 = task_store.enqueue_task_sync(op2)
        task_store.mark_task_running_sync(t2.id)

        # 3. Simulate process crash and restart recovery
        interrupted, queued = task_store.recover_on_startup_sync()
        assert interrupted == 1
        assert queued == 1

        # Check records
        rec1 = task_store.get_task_sync(t1.id)
        assert rec1 is not None
        assert rec1.status == TaskStatus.QUEUED  # preserved!

        rec2 = task_store.get_task_sync(t2.id)
        assert rec2 is not None
        assert rec2.status == TaskStatus.INTERRUPTED  # marked interrupted
        assert "перезапуском" in (rec2.error_message or "")

    def test_interrupted_tasks_are_not_automatically_requeued(
        self, task_store: TaskStore
    ) -> None:
        """Req 11: INTERRUPTED tasks are not returned by get_next_queued_task."""
        op = _make_pending_confirmation(op_id="op-crash")
        t = task_store.enqueue_task_sync(op)
        task_store.mark_task_running_sync(t.id)

        task_store.recover_on_startup_sync()

        # No queued task should be returned
        next_task = task_store.get_next_queued_task_sync()
        assert next_task is None


class TestTaskCancellationInStore:
    """Tests for cancelling tasks directly in SQLite."""

    def test_cancel_queued_task_success(self, task_store: TaskStore) -> None:
        """Req 12: Queued task can be cancelled by its owner."""
        op = _make_pending_confirmation(op_id="op-cancel", user_id=111222)
        task = task_store.enqueue_task_sync(op)

        success, msg, updated = task_store.cancel_queued_task_sync(task.id, user_id=111222)
        assert success is True
        assert updated is not None
        assert updated.status == TaskStatus.CANCELED

        # It should no longer be eligible for queue execution
        assert task_store.get_next_queued_task_sync() is None

    def test_cancel_other_user_task_forbidden(self, task_store: TaskStore) -> None:
        """Req 15: Cannot cancel task belonging to another user."""
        op = _make_pending_confirmation(op_id="op-other", user_id=111222)
        task = task_store.enqueue_task_sync(op)

        success, msg, _ = task_store.cancel_queued_task_sync(task.id, user_id=999999)
        assert success is False
        assert "не являетесь владельцем" in msg


class TestResultFileStorage:
    """Tests for result file persistence and path boundary checks."""

    def test_save_and_read_result_file(self, task_store: TaskStore) -> None:
        """Save output and read it back safely."""
        content = "Important output from task\nSecond line"
        path = task_store.save_result_file(task_id=42, content=content)

        assert path.is_file()
        assert path.name == "task_42.txt"

        read_back = task_store.read_result_file(task_id=42)
        assert read_back == content

    def test_result_size_truncation(self, task_store: TaskStore) -> None:
        """Outputs exceeding max size are truncated safely."""
        oversized = "A" * (11 * 1024 * 1024)  # 11 MB
        path = task_store.save_result_file(task_id=99, content=oversized)
        assert path.is_file()

        read_back = task_store.read_result_file(task_id=99)
        assert read_back is not None
        assert "[... Truncated due to size limit ...]" in read_back

    def test_async_save_and_read_result_file(self, task_store: TaskStore) -> None:
        """Stage 6.2: Asynchronously save and read output file."""
        import asyncio

        async def _test() -> None:
            content = "Async output data\nLine 2"
            path = await task_store.save_result_file_async(task_id=77, content=content)
            assert path.is_file()

            read_back = await task_store.read_result_file_async(task_id=77)
            assert read_back == content

        asyncio.run(_test())


class TestSchemaMigrationAndQueueLimits:
    """Stage 8: Tests for real v1 -> v2 schema migration and queue limits."""

    def test_schema_migration_v1_to_v2(self, tmp_path: Path) -> None:
        """Stage 8.2: Test database migration from v1 to v2 updates indexes."""
        db_path = tmp_path / "v1_tasks.sqlite3"
        results_dir = tmp_path / "v1_results"
        results_dir.mkdir(parents=True, exist_ok=True)

        # Create a database simulating v1 schema
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE tasks (
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
            cursor.execute("CREATE INDEX idx_tasks_status ON tasks(status)")
            cursor.execute("CREATE INDEX idx_tasks_user_id ON tasks(user_id, id DESC)")
            cursor.execute("CREATE INDEX idx_tasks_operation_id ON tasks(operation_id)")
            cursor.execute("PRAGMA user_version = 1")
            conn.commit()

        # Initialize store on existing v1 database
        store = TaskStore(db_path, results_dir)
        store.initialize_sync()

        # Verify upgraded to v2
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 2

            # Check that idx_tasks_operation_id is removed and idx_tasks_status_id is created
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tasks'"
            )
            indexes = [row[0] for row in cursor.fetchall()]
            assert "idx_tasks_status_id" in indexes
            assert "idx_tasks_operation_id" not in indexes
            assert "idx_tasks_status" not in indexes

    def test_max_queue_size_enforced(self, tmp_path: Path) -> None:
        """Stage 8.2: Enforce max_queue_size limit when enqueuing tasks."""
        db_path = tmp_path / "queue_limit.sqlite3"
        results_dir = tmp_path / "limit_results"
        store = TaskStore(db_path, results_dir, max_queue_size=2)
        store.initialize_sync()

        # Enqueue 2 tasks (reaches limit)
        store.enqueue_task_sync(_make_pending_confirmation("op-limit-1", "Task 1"))
        store.enqueue_task_sync(_make_pending_confirmation("op-limit-2", "Task 2"))

        # Third task should raise QueueFullError
        with pytest.raises(QueueFullError) as exc_info:
            store.enqueue_task_sync(_make_pending_confirmation("op-limit-3", "Task 3"))

        assert "Очередь задач заполнена (2/2)" in str(exc_info.value)

