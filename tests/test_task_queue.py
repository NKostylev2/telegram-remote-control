"""Unit tests for TaskQueue worker, sequential execution, concurrency, and cancellation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from remote_control.agent.agy_client import AgyClient
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.controller.confirmation import PendingConfirmation
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import TaskStore


@pytest.fixture
def test_store(tmp_path: Path) -> TaskStore:
    db = tmp_path / "tasks.sqlite3"
    results = tmp_path / "results"
    store = TaskStore(db, results)
    store.initialize_sync()
    return store


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=AgyClient)
    client.execute = AsyncMock(
        return_value=TaskResult(
            task_id=uuid4(),
            status=TaskStatus.SUCCESS,
            response="Done successfully.",
            duration_seconds=1.5,
        )
    )
    return client


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock(spec=Bot)
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


def _make_op(op_id: str, prompt: str = "Test", user_id: int = 12345) -> PendingConfirmation:
    return PendingConfirmation(
        operation_id=op_id,
        user_id=user_id,
        chat_id=user_id,
        workspace_alias="default",
        prompt=prompt,
        created_at=100.0,
        expires_at=200.0,
    )


class TestTaskQueueSequentialExecution:
    """Tests for FIFO ordering, sequential processing, and single-concurrency."""

    def test_tasks_executed_sequentially_in_fifo_order(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 5 & 6: Multiple tasks are executed one-by-one in FIFO queue order."""
        async def _test() -> None:
            execution_order: list[str] = []

            async def fake_execute(prompt: str, **kwargs: object) -> TaskResult:
                execution_order.append(prompt)
                await asyncio.sleep(0.05)
                return TaskResult(
                    task_id=uuid4(),
                    status=TaskStatus.SUCCESS,
                    response=f"Done {prompt}",
                    duration_seconds=0.05,
                )

            mock_client.execute = fake_execute

            # Enqueue 3 tasks in order
            t1 = await test_store.enqueue_task(_make_op("op-1", prompt="Task 1"))
            t2 = await test_store.enqueue_task(_make_op("op-2", prompt="Task 2"))
            t3 = await test_store.enqueue_task(_make_op("op-3", prompt="Task 3"))

            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            # Wait for all 3 tasks to finish
            for _ in range(30):
                r3 = await test_store.get_task(t3.id)
                if r3 and r3.status == TaskStatus.SUCCESS:
                    break
                await asyncio.sleep(0.1)

            await queue.stop()

            assert execution_order == ["Task 1", "Task 2", "Task 3"]

            # Check final DB statuses
            rec1 = await test_store.get_task(t1.id)
            rec2 = await test_store.get_task(t2.id)
            rec3 = await test_store.get_task(t3.id)
            assert rec1 is not None and rec1.status == TaskStatus.SUCCESS
            assert rec2 is not None and rec2.status == TaskStatus.SUCCESS
            assert rec3 is not None and rec3.status == TaskStatus.SUCCESS

        asyncio.run(_test())

    def test_single_concurrency_no_parallel_runs(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 7: Only one task runs at any given time (concurrency <= 1)."""
        async def _test() -> None:
            concurrent_runs = 0
            max_concurrent_observed = 0

            async def fake_execute(*args: object, **kwargs: object) -> TaskResult:
                nonlocal concurrent_runs, max_concurrent_observed
                concurrent_runs += 1
                max_concurrent_observed = max(max_concurrent_observed, concurrent_runs)
                await asyncio.sleep(0.1)
                concurrent_runs -= 1
                return TaskResult(
                    task_id=uuid4(),
                    status=TaskStatus.SUCCESS,
                    response="ok",
                    duration_seconds=0.1,
                )

            mock_client.execute = fake_execute

            await test_store.enqueue_task(_make_op("p1", "Prompt 1"))
            t2 = await test_store.enqueue_task(_make_op("p2", "Prompt 2"))

            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            for _ in range(25):
                r2 = await test_store.get_task(t2.id)
                if r2 and r2.status == TaskStatus.SUCCESS:
                    break
                await asyncio.sleep(0.1)

            await queue.stop()

            assert max_concurrent_observed == 1

        asyncio.run(_test())


class TestTaskQueueCancellation:
    """Tests for cancelling running tasks and race conditions."""

    def test_cancel_running_task(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 13: Cancelling a running task terminates CLI execution and records CANCELED."""
        async def _test() -> None:
            started_event = asyncio.Event()

            async def slow_execute(*args: object, **kwargs: object) -> TaskResult:
                started_event.set()
                try:
                    await asyncio.sleep(5.0)  # long-running
                except asyncio.CancelledError:
                    return TaskResult(
                        task_id=uuid4(),
                        status=TaskStatus.CANCELED,
                        exit_code=130,
                        error="Task was canceled",
                    )
                return TaskResult(task_id=uuid4(), status=TaskStatus.SUCCESS)

            mock_client.execute = slow_execute

            task = await test_store.enqueue_task(_make_op("op-slow", "Long task", user_id=111))
            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            # Wait until it enters execute
            await asyncio.wait_for(started_event.wait(), timeout=2.0)

            # Now cancel the task
            success, msg, rec = await queue.cancel_task(task.id, user_id=111)
            assert success is True
            assert "отменена" in msg

            # DB record is updated to CANCELED
            final_rec = await test_store.get_task(task.id)
            assert final_rec is not None
            assert final_rec.status == TaskStatus.CANCELED

            await queue.stop()

        asyncio.run(_test())

    def test_race_condition_cancel_when_already_finished(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 14: Cancelling after task has already completed returns finished status."""
        async def _test() -> None:
            task = await test_store.enqueue_task(_make_op("op-fast", "Fast task", user_id=111))
            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            for _ in range(20):
                r = await test_store.get_task(task.id)
                if r and r.status == TaskStatus.SUCCESS:
                    break
                await asyncio.sleep(0.1)

            # Try to cancel after it completed
            success, msg, rec = await queue.cancel_task(task.id, user_id=111)
            assert success is False
            assert "успешно выполнена" in msg
            assert rec is not None
            assert rec.status == TaskStatus.SUCCESS

            await queue.stop()

        asyncio.run(_test())


class TestTaskQueueErrorHandling:
    """Tests for CLI errors, soft-denied tools, and Telegram API failures."""

    def test_cli_error_and_partial_success_saved_to_db(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 19: CLI errors and PARTIAL_SUCCESS are properly recorded in SQLite."""
        async def _test() -> None:
            mock_client.execute = AsyncMock(
                return_value=TaskResult(
                    task_id=uuid4(),
                    status=TaskStatus.PARTIAL_SUCCESS,
                    response="File created with limitation",
                    duration_seconds=2.1,
                    soft_denied_tools=["shell_exec"],
                    warnings=["tool permission denied"],
                )
            )

            task = await test_store.enqueue_task(_make_op("op-partial", "Test partial"))
            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            for _ in range(20):
                r = await test_store.get_task(task.id)
                if r and r.status == TaskStatus.PARTIAL_SUCCESS:
                    break
                await asyncio.sleep(0.1)

            await queue.stop()

            final_rec = await test_store.get_task(task.id)
            assert final_rec is not None
            assert final_rec.status == TaskStatus.PARTIAL_SUCCESS
            assert "shell_exec" in final_rec.soft_denied_tools
            assert "tool permission denied" in final_rec.warnings

        asyncio.run(_test())

    def test_telegram_send_failure_does_not_fail_task(
        self, test_store: TaskStore, mock_client: MagicMock, mock_bot: MagicMock
    ) -> None:
        """Req 18: Telegram delivery failures do not corrupt task status or abort queue."""
        async def _test() -> None:
            # Simulate Telegram network error
            mock_bot.send_message.side_effect = TelegramAPIError(
                method="sendMessage", message="Bot was blocked by user"
            )

            task = await test_store.enqueue_task(_make_op("op-tg-err", "Task 1"))
            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            for _ in range(20):
                r = await test_store.get_task(task.id)
                if r and r.status == TaskStatus.SUCCESS:
                    break
                await asyncio.sleep(0.1)

            await queue.stop()

            final_rec = await test_store.get_task(task.id)
            assert final_rec is not None
            assert final_rec.status == TaskStatus.SUCCESS  # Success in CLI & DB despite TG failure!

        asyncio.run(_test())

    def test_no_secrets_in_task_queue_logs(
        self,
        test_store: TaskStore,
        mock_client: MagicMock,
        mock_bot: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Req 23: Verification that prompt secrets are not printed in info logs."""
        async def _test() -> None:
            caplog.set_level(logging.INFO)
            secret_word = "SUPER_SECRET_PASSWORD_12345"

            task = await test_store.enqueue_task(
                _make_op("op-secret", f"Process with {secret_word}")
            )
            queue = TaskQueue(task_store=test_store, agy_client=mock_client, bot=mock_bot)
            queue.start()

            for _ in range(20):
                r = await test_store.get_task(task.id)
                if r and r.status == TaskStatus.SUCCESS:
                    break
                await asyncio.sleep(0.1)

            await queue.stop()

            # Verify prompt content does not appear in standard INFO logs
            for record in caplog.records:
                if record.name.startswith("remote_control.controller.task_queue"):
                    assert secret_word not in record.getMessage()

        asyncio.run(_test())
