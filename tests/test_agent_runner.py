"""Unit tests for Windows Agent runner, lifecycle loops, and task execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from remote_control.agent.agy_client import AgyClient
from remote_control.agent.client import AgentHttpClient
from remote_control.agent.runner import AgentRunner
from remote_control.agent.screenshot import ScreenshotResult
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.common.protocol import (
    AgentTaskAssignment,
    HeartbeatResponse,
    TaskResultSubmissionResponse,
    TaskStatusUpdateResponse,
)
from remote_control.config import AppConfig


class TestAgentRunnerExecution:
    """Tests for AgentRunner execution logic and interactions."""

    def test_get_available_workspaces_checks_disk(self, tmp_path: Path) -> None:
        existing_dir = tmp_path / "valid_ws"
        existing_dir.mkdir()
        missing_dir = tmp_path / "missing_ws"

        config = AppConfig(
            ALLOWED_WORKSPACES={
                "valid": existing_dir,
                "missing": missing_dir,
            }
        )
        runner = AgentRunner(config=config, agent_id="test-agent")
        workspaces = runner.get_available_workspaces()

        assert "valid" in workspaces
        assert "missing" not in workspaces
        assert workspaces["valid"] == str(existing_dir.resolve())

    def test_heartbeat_handles_cancel_instruction(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.send_heartbeat = AsyncMock(
                return_value=HeartbeatResponse(
                    acknowledged=True,
                    cancel_task_ids=[101],
                    screenshot_requested=False,
                )
            )

            mock_agy = MagicMock(spec=AgyClient)
            mock_agy.is_cli_available.return_value = True

            runner = AgentRunner(config=config, client=mock_client, agy_client=mock_agy)
            assignment = AgentTaskAssignment(
                task_id=101,
                operation_id="op_101",
                workspace_alias="ws",
                prompt="test prompt",
            )
            runner._current_task_assignment = assignment

            # Create mock running execution task
            fake_fut = asyncio.get_running_loop().create_future()
            runner._current_exec_task = fake_fut

            await runner._heartbeat_once()

            assert fake_fut.cancelled()
            mock_client.send_heartbeat.assert_awaited_once()

        asyncio.run(_test())

    def test_heartbeat_triggers_screenshot_capture(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.send_heartbeat = AsyncMock(
                return_value=HeartbeatResponse(
                    acknowledged=True,
                    screenshot_requested=True,
                )
            )
            mock_client.upload_screenshot = AsyncMock(return_value=True)

            mock_agy = MagicMock(spec=AgyClient)
            mock_agy.is_cli_available.return_value = True

            runner = AgentRunner(config=config, client=mock_client, agy_client=mock_agy)

            dummy_screenshot = ScreenshotResult(
                image_bytes=b"\xff\xd8fake_jpeg",
                width=1920,
                height=1080,
                timestamp=MagicMock(),
            )

            with patch(
                "remote_control.agent.runner.capture_desktop_screenshot",
                return_value=dummy_screenshot,
            ):
                await runner._heartbeat_once()
                mock_client.upload_screenshot.assert_awaited_once_with(b"\xff\xd8fake_jpeg")

        asyncio.run(_test())

    def test_execute_assigned_task_lifecycle(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.update_task_status = AsyncMock(
                return_value=TaskStatusUpdateResponse(acknowledged=True)
            )
            mock_client.submit_task_result = AsyncMock(
                return_value=TaskResultSubmissionResponse(acknowledged=True, task_id=202)
            )

            mock_agy = MagicMock(spec=AgyClient)
            dummy_res = TaskResult(
                task_id="op_202",
                status=TaskStatus.SUCCESS,
                exit_code=0,
                response="Finished OK",
                duration_seconds=2.5,
            )
            mock_agy.execute = AsyncMock(return_value=dummy_res)

            runner = AgentRunner(config=config, client=mock_client, agy_client=mock_agy)
            assignment = AgentTaskAssignment(
                task_id=202,
                operation_id="op_202",
                workspace_alias="ws",
                workspace_path=None,
                prompt="git status",
            )

            await runner._execute_assigned_task(assignment)

            mock_client.update_task_status.assert_awaited_once()
            status_arg = mock_client.update_task_status.call_args[0][0]
            assert status_arg.task_id == 202
            assert status_arg.status == TaskStatus.RUNNING

            mock_agy.execute.assert_awaited_once_with(
                prompt="git status",
                workspace_alias="ws",
                workspace_path=None,
                effort="low",
                dangerously_skip_permissions=True,
            )

            mock_client.submit_task_result.assert_awaited_once()
            sub_arg = mock_client.submit_task_result.call_args[0][0]
            assert sub_arg.task_id == 202
            assert sub_arg.status == TaskStatus.SUCCESS
            assert sub_arg.response == "Finished OK"

        asyncio.run(_test())

    def test_execute_assigned_task_handles_cancellation(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.update_task_status = AsyncMock(
                return_value=TaskStatusUpdateResponse(acknowledged=True)
            )
            mock_client.submit_task_result = AsyncMock(
                return_value=TaskResultSubmissionResponse(acknowledged=True, task_id=303)
            )

            mock_agy = MagicMock(spec=AgyClient)

            async def slow_exec(*args, **kwargs):
                await asyncio.sleep(10.0)
                return TaskResult(task_id="op_303", status=TaskStatus.SUCCESS)

            mock_agy.execute = AsyncMock(side_effect=slow_exec)

            runner = AgentRunner(config=config, client=mock_client, agy_client=mock_agy)
            assignment = AgentTaskAssignment(
                task_id=303,
                operation_id="op_303",
                workspace_alias="ws",
                prompt="long task",
            )

            exec_coro = asyncio.create_task(runner._execute_assigned_task(assignment))
            await asyncio.sleep(0.02)

            # Cancel running execution
            async with runner._cancel_lock:
                if runner._current_exec_task:
                    runner._current_exec_task.cancel()

            await exec_coro

            mock_client.submit_task_result.assert_awaited_once()
            sub_arg = mock_client.submit_task_result.call_args[0][0]
            assert sub_arg.task_id == 303
            assert sub_arg.status == TaskStatus.CANCELED

        asyncio.run(_test())
