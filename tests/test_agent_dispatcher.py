"""Unit tests for AgentDispatcher (state tracking, task routing, heartbeats, and screenshots)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from remote_control.common.models import TaskStatus
from remote_control.common.protocol import (
    HeartbeatRequest,
    TaskResultSubmission,
    TaskStatusUpdateRequest,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.task_store import TaskRecord


def _make_dummy_task(task_id: int = 1) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        id=task_id,
        operation_id=f"op_{task_id}",
        user_id=123,
        chat_id=123,
        workspace_alias="test_ws",
        workspace_path=Path("/tmp/ws"),
        prompt="run tests",
        interpretation="Run unit tests",
        effort="low",
        status=TaskStatus.QUEUED,
        created_at=now,
        confirmed_at=now,
        queued_at=now,
    )


class TestAgentDispatcherLifecycle:
    """Tests for agent connection, capability detection, and offline timeouts."""

    def test_initial_state_offline(self) -> None:
        config = AppConfig()
        dispatcher = AgentDispatcher(config=config, heartbeat_timeout=1.0)

        assert dispatcher.is_agent_connected() is False
        assert dispatcher.is_agent_ready() is False
        assert dispatcher.get_agent_info() is None

    def test_heartbeat_registers_agent_and_enables_ready_state(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config=config, heartbeat_timeout=2.0)

            req = HeartbeatRequest(
                agent_id="win-1",
                hostname="MY-DESKTOP",
                cli_available=True,
                cli_version="1.3.2",
                available_workspaces={"test": "C:\\test"},
            )
            resp = await dispatcher.handle_heartbeat(req)
            assert resp.acknowledged is True

            assert dispatcher.is_agent_connected() is True
            assert dispatcher.is_agent_ready() is True

            info = dispatcher.get_agent_info()
            assert info is not None
            assert info.agent_id == "win-1"
            assert info.hostname == "MY-DESKTOP"
            assert info.cli_available is True

        asyncio.run(_test())

    def test_heartbeat_timeout_transitions_to_offline(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            # Very short timeout for testing
            dispatcher = AgentDispatcher(config=config, heartbeat_timeout=0.05)

            req = HeartbeatRequest(
                agent_id="win-1",
                hostname="MY-DESKTOP",
                cli_available=True,
            )
            await dispatcher.handle_heartbeat(req)
            assert dispatcher.is_agent_connected() is True

            # Wait beyond timeout
            await asyncio.sleep(0.08)
            assert dispatcher.is_agent_connected() is False
            assert dispatcher.is_agent_ready() is False

        asyncio.run(_test())


class TestAgentDispatcherTaskRouting:
    """Tests for task dispatching, polling, and result reception."""

    def test_dispatch_when_agent_offline_returns_error(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config=config)
            task = _make_dummy_task(1)

            result = await dispatcher.dispatch_and_wait(task)
            assert result.status == TaskStatus.ERROR
            assert "Windows Agent недоступен" in result.error

        asyncio.run(_test())

    def test_dispatch_poll_and_result_success_lifecycle(self) -> None:
        async def _test() -> None:
            config = AppConfig(TASK_EXECUTION_TIMEOUT_SECONDS=10)
            dispatcher = AgentDispatcher(config=config)

            # 1. Register agent
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )

            # 2. Dispatch task in background
            task = _make_dummy_task(42)
            dispatch_coro = asyncio.create_task(dispatcher.dispatch_and_wait(task))
            await asyncio.sleep(0.01)

            # 3. Agent polls tasks
            poll_resp = await dispatcher.poll_tasks(agent_id="win-1", max_tasks=1)
            assert len(poll_resp.tasks) == 1
            assert poll_resp.tasks[0].task_id == 42
            assert poll_resp.tasks[0].prompt == "run tests"

            # 4. Agent updates status
            status_resp = await dispatcher.update_task_status(
                TaskStatusUpdateRequest(
                    agent_id="win-1",
                    task_id=42,
                    status=TaskStatus.RUNNING,
                )
            )
            assert status_resp.acknowledged is True
            assert status_resp.cancel_requested is False

            # 5. Agent submits result
            sub_resp = await dispatcher.submit_task_result(
                TaskResultSubmission(
                    agent_id="win-1",
                    task_id=42,
                    status=TaskStatus.SUCCESS,
                    exit_code=0,
                    response="Tests passed 100%",
                    duration_seconds=3.14,
                )
            )
            assert sub_resp.acknowledged is True
            assert sub_resp.task_id == 42

            # 6. Dispatch coro finishes and returns result
            final_result = await dispatch_coro
            assert final_result.status == TaskStatus.SUCCESS
            assert final_result.response == "Tests passed 100%"
            assert final_result.duration_seconds == 3.14

        asyncio.run(_test())

    def test_task_cancellation_flow(self) -> None:
        async def _test() -> None:
            config = AppConfig(TASK_EXECUTION_TIMEOUT_SECONDS=10)
            dispatcher = AgentDispatcher(config=config)

            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )

            task = _make_dummy_task(77)
            dispatch_coro = asyncio.create_task(dispatcher.dispatch_and_wait(task))
            await asyncio.sleep(0.01)

            # Request cancellation
            cancelled = await dispatcher.request_task_cancellation(77)
            assert cancelled is True

            # Agent checks status update or heartbeat and sees cancel flag
            hb_resp = await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )
            assert 77 in hb_resp.cancel_task_ids

            final_result = await dispatch_coro
            assert final_result.status == TaskStatus.CANCELED

        asyncio.run(_test())


class TestAgentDispatcherScreenshotAndWake:
    """Tests for screenshot transfer and wake online synchronization."""

    def test_screenshot_request_and_upload(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config=config)

            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )

            screenshot_coro = asyncio.create_task(
                dispatcher.request_screenshot(timeout_seconds=5.0)
            )
            await asyncio.sleep(0.01)

            # Heartbeat shows screenshot requested
            hb_resp = await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )
            assert hb_resp.screenshot_requested is True

            # Agent uploads screenshot bytes
            fake_jpeg = b"\xff\xd8\xff\xe0fake_jpeg_content"
            submitted = await dispatcher.submit_screenshot(fake_jpeg)
            assert submitted is True

            received_bytes = await screenshot_coro
            assert received_bytes == fake_jpeg

        asyncio.run(_test())

    def test_wait_for_agent_online_succeeds_on_heartbeat(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config=config)

            wait_coro = asyncio.create_task(
                dispatcher.wait_for_agent_online(timeout_seconds=2.0)
            )
            await asyncio.sleep(0.01)

            # Agent arrives and sends heartbeat
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="win-1", hostname="PC", cli_available=True)
            )

            is_online = await wait_coro
            assert is_online is True

        asyncio.run(_test())

    def test_wait_for_agent_online_times_out_if_no_heartbeat(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config=config)

            is_online = await dispatcher.wait_for_agent_online(timeout_seconds=0.05)
            assert is_online is False

        asyncio.run(_test())
