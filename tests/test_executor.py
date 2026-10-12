"""Unit tests for TaskExecutor abstraction layer (LocalTaskExecutor and RemoteTaskExecutor)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from remote_control.agent.agy_client import AgyClient
from remote_control.common.executor import LocalTaskExecutor, RemoteTaskExecutor
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.controller.task_store import TaskRecord


def _make_dummy_task_record(task_id: int = 1) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        id=task_id,
        operation_id="op_test_123",
        user_id=1001,
        chat_id=2001,
        workspace_alias="test_ws",
        workspace_path=Path("/dummy/path"),
        prompt="echo test",
        interpretation="Test interpretation",
        effort="low",
        status=TaskStatus.QUEUED,
        created_at=now,
        confirmed_at=now,
        queued_at=now,
    )


class TestLocalTaskExecutor:
    """Tests for LocalTaskExecutor wrapping AgyClient."""

    def test_is_available_delegates_to_agy_client(self) -> None:
        mock_agy = MagicMock(spec=AgyClient)
        mock_agy.is_cli_available.return_value = True

        executor = LocalTaskExecutor(mock_agy)
        assert executor.is_available() is True
        mock_agy.is_cli_available.assert_called_once()

    def test_execute_task_success(self) -> None:
        async def _test() -> None:
            mock_agy = MagicMock(spec=AgyClient)
            dummy_result = TaskResult(
                task_id="op_test_123",
                status=TaskStatus.SUCCESS,
                exit_code=0,
                response="Done",
                duration_seconds=1.5,
            )
            mock_agy.execute = AsyncMock(return_value=dummy_result)

            executor = LocalTaskExecutor(mock_agy)
            task = _make_dummy_task_record(1)

            result = await executor.execute_task(task)
            assert result.status == TaskStatus.SUCCESS
            assert result.response == "Done"
            mock_agy.execute.assert_awaited_once_with(
                prompt="echo test",
                workspace_alias=None,
                workspace_path=Path("/dummy/path"),
                effort="low",
            )

        asyncio.run(_test())

    def test_execute_task_cancellation(self) -> None:
        async def _test() -> None:
            mock_agy = MagicMock(spec=AgyClient)

            async def slow_execute(*args, **kwargs):
                await asyncio.sleep(10.0)
                return TaskResult(task_id="op_test_123", status=TaskStatus.SUCCESS)

            mock_agy.execute = AsyncMock(side_effect=slow_execute)

            executor = LocalTaskExecutor(mock_agy)
            task = _make_dummy_task_record(1)

            exec_coro = asyncio.create_task(executor.execute_task(task))
            await asyncio.sleep(0.05)

            # Cancel the task
            cancelled = await executor.cancel_task(task)
            assert cancelled is True

            result = await exec_coro
            assert result.status == TaskStatus.CANCELED
            assert result.exit_code == 130

        asyncio.run(_test())


class TestRemoteTaskExecutor:
    """Tests for RemoteTaskExecutor delegating to AgentDispatcher."""

    def test_is_available_delegates_to_dispatcher(self) -> None:
        dispatcher = MagicMock()
        dispatcher.is_agent_ready.return_value = True

        executor = RemoteTaskExecutor(dispatcher)
        assert executor.is_available() is True
        dispatcher.is_agent_ready.assert_called_once()

    def test_execute_task_delegates_to_dispatcher(self) -> None:
        async def _test() -> None:
            dispatcher = MagicMock()
            dummy_result = TaskResult(
                task_id="op_test_123",
                status=TaskStatus.SUCCESS,
                exit_code=0,
                response="Remote execution finished",
                duration_seconds=2.0,
            )
            dispatcher.dispatch_and_wait = AsyncMock(return_value=dummy_result)

            executor = RemoteTaskExecutor(dispatcher)
            task = _make_dummy_task_record(2)

            result = await executor.execute_task(task)
            assert result.status == TaskStatus.SUCCESS
            assert result.response == "Remote execution finished"
            dispatcher.dispatch_and_wait.assert_awaited_once_with(task)

        asyncio.run(_test())

    def test_cancel_task_delegates_to_dispatcher(self) -> None:
        async def _test() -> None:
            dispatcher = MagicMock()
            dispatcher.request_task_cancellation = AsyncMock(return_value=True)

            executor = RemoteTaskExecutor(dispatcher)
            task = _make_dummy_task_record(3)

            cancelled = await executor.cancel_task(task)
            assert cancelled is True
            dispatcher.request_task_cancellation.assert_awaited_once_with(3)

        asyncio.run(_test())
