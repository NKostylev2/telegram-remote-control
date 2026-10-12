"""Unit tests for distributed mode handlers.

Tests cmd_status, cmd_workspace, cmd_wake, cmd_screenshot, and cmd_run in distributed mode.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Chat, Message, User

from remote_control.agent.agy_client import AgyClient
from remote_control.common.executor import RemoteTaskExecutor
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.common.protocol import AgentStatusInfo
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.confirmation import ConfirmationStore
from remote_control.controller.handlers.base import cmd_status, cmd_wake, cmd_workspace
from remote_control.controller.handlers.run import cmd_run
from remote_control.controller.handlers.screenshot import cmd_screenshot
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import TaskStore


def _make_msg(user_id: int = 111222333, text: str = "test") -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=user_id, is_bot=False, first_name="Kostya")
    msg.chat = Chat(id=user_id, type="private")
    msg.text = text
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.answer_photo = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


def test_status_command_distributed_online() -> None:
    """Test /status shows Windows Agent online and CLI details when connected."""
    async def _test() -> None:
        cfg = AppConfig(
            TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            ALLOWED_WORKSPACES={"project": "C:\\Workspace\\project"},
        )
        dispatcher = AgentDispatcher(cfg)
        dispatcher._current_agent = AgentStatusInfo(
            agent_id="test-agent",
            hostname="DESKTOP-WIN",
            platform="Windows-11",
            cli_available=True,
            cli_version="0.2.1",
            available_workspaces={"project": "C:\\Workspace\\project"},
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        dispatcher._last_heartbeat_monotonic = time.monotonic()

        msg = _make_msg()
        mock_client = MagicMock(spec=AgyClient)
        await cmd_status(msg, config=cfg, agy_client=mock_client, agent_dispatcher=dispatcher)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        assert "Распределённый Controller" in text
        assert "🟢 Онлайн" in text
        assert "DESKTOP-WIN" in text
        assert "0.2.1" in text

    asyncio.run(_test())


def test_status_command_distributed_offline() -> None:
    """Test /status shows Windows Agent offline and suggestion to use /wake."""
    async def _test() -> None:
        cfg = AppConfig(
            TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
        )
        dispatcher = AgentDispatcher(cfg)
        msg = _make_msg()
        mock_client = MagicMock(spec=AgyClient)
        await cmd_status(msg, config=cfg, agy_client=mock_client, agent_dispatcher=dispatcher)

        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        assert "Распределённый Controller" in text
        assert "🔴 Оффлайн" in text
        assert "/wake" in text

    asyncio.run(_test())


def test_workspace_command_distributed_online_and_offline() -> None:
    """Test /workspace marks workspaces with Windows status and hides absolute paths."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            ALLOWED_WORKSPACES={"project1": "C:\\Workspace\\project1"},
        )
        dispatcher = AgentDispatcher(cfg)

        # 1. Offline check
        msg1 = _make_msg()
        await cmd_workspace(msg1, config=cfg, agent_dispatcher=dispatcher)
        text1 = msg1.answer.call_args[0][0]
        assert "project1" in text1
        assert "Windows Agent оффлайн" in text1
        assert "C:\\Workspace\\project1" not in text1

        # 2. Online check
        dispatcher._current_agent = AgentStatusInfo(
            agent_id="test-agent",
            hostname="DESKTOP-WIN",
            platform="Windows",
            cli_available=True,
            available_workspaces={"project1": "C:\\Workspace\\project1"},
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        dispatcher._last_heartbeat_monotonic = time.monotonic()

        msg2 = _make_msg()
        await cmd_workspace(msg2, config=cfg, agent_dispatcher=dispatcher)
        text2 = msg2.answer.call_args[0][0]
        assert "project1" in text2
        assert "🟢 Доступно" in text2
        assert "C:\\Workspace\\project1" not in text2

    asyncio.run(_test())


def test_wake_command_distributed_agent_online() -> None:
    """Test /wake in distributed mode notices when agent is already online."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            TARGET_MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
        )
        dispatcher = AgentDispatcher(cfg)
        dispatcher._current_agent = AgentStatusInfo(
            agent_id="test-agent",
            hostname="DESKTOP-WIN",
            platform="Windows",
            cli_available=True,
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        dispatcher._last_heartbeat_monotonic = time.monotonic()

        msg = _make_msg()
        await cmd_wake(msg, config=cfg, agent_dispatcher=dispatcher)
        text = msg.answer.call_args[0][0]
        assert "уже включён и доступен" in text
        assert "Windows Agent подключён" in text

    asyncio.run(_test())


def test_wake_command_distributed_waits_for_agent() -> None:
    """Test /wake in distributed mode sends WoL packet and awaits AgentDispatcher online event."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            TARGET_MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
        )
        dispatcher = AgentDispatcher(cfg)
        msg = _make_msg()
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        msg.answer.return_value = status_msg

        with (
            patch(
                "remote_control.controller.handlers.base.check_host_availability",
                return_value=False,
            ),
            patch("remote_control.controller.handlers.base.send_wake_on_lan", return_value=3),
            patch.object(dispatcher, "wait_for_agent_online", AsyncMock(return_value=True)),
        ):
            await cmd_wake(msg, config=cfg, agent_dispatcher=dispatcher)
            assert status_msg.edit_text.await_count == 2
            final_text = status_msg.edit_text.call_args[0][0]
            assert "Windows Agent подключился к Android Controller" in final_text

    asyncio.run(_test())


def test_screenshot_command_distributed_mode() -> None:
    """Test /screenshot queries AgentDispatcher in distributed mode."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
        )
        dispatcher = AgentDispatcher(cfg)

        # 1. Offline rejection
        msg_offline = _make_msg()
        await cmd_screenshot(msg_offline, config=cfg, agent_dispatcher=dispatcher)
        text_offline = msg_offline.answer.call_args[0][0]
        assert "Windows Agent оффлайн" in text_offline

        # 2. Online screenshot request
        dispatcher._current_agent = AgentStatusInfo(
            agent_id="test-agent",
            hostname="DESKTOP-WIN",
            platform="Windows",
            cli_available=True,
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        dispatcher._last_heartbeat_monotonic = time.monotonic()

        with patch.object(
            dispatcher,
            "request_screenshot",
            AsyncMock(return_value=b"\xff\xd8\xfffakeimage"),
        ):
            msg_online = _make_msg()
            wait_msg = MagicMock()
            wait_msg.delete = AsyncMock()
            msg_online.answer.return_value = wait_msg

            await cmd_screenshot(msg_online, config=cfg, agent_dispatcher=dispatcher)
            msg_online.answer_photo.assert_awaited_once()
            caption = msg_online.answer_photo.call_args[1]["caption"]
            assert "1920x1080" in caption

    asyncio.run(_test())


def test_run_command_distributed_mode_enqueues_without_local_cli() -> None:
    """Test /run fast-track in distributed mode does not require local CLI on controller."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            ALLOWED_WORKSPACES={"project": "C:\\Workspace\\project"},
        )
        dispatcher = AgentDispatcher(cfg)
        mock_client = MagicMock(spec=AgyClient)
        mock_client.is_cli_available.return_value = False  # Local CLI is absent on Android!

        store = ConfirmationStore()
        msg = _make_msg(text="/run project Проверь статус")
        cmd_obj = MagicMock()
        cmd_obj.args = "project Проверь статус"

        await cmd_run(
            message=msg,
            command=cmd_obj,
            config=cfg,
            agy_client=mock_client,
            confirmation_store=store,
            agent_dispatcher=dispatcher,
        )

        # Must NOT fail with "CLI не найден", must prompt for confirmation
        msg.answer.assert_awaited_once()
        text = msg.answer.call_args[0][0]
        assert "Правильно ли агент понял задачу?" in text
        assert len(store._operations) == 1

    asyncio.run(_test())


def test_task_queue_waits_for_agent_and_resumes(tmp_path: Path) -> None:
    """Test TaskQueue leaves task in QUEUED while offline and resumes when agent is ready."""
    async def _test() -> None:
        cfg = AppConfig(
            ALLOWED_TELEGRAM_USERS=[111222333],
            CONTROLLER_MODE="distributed",
            ALLOWED_WORKSPACES={"project": "C:\\Workspace\\project"},
        )
        dispatcher = AgentDispatcher(cfg)
        executor = RemoteTaskExecutor(dispatcher)

        db_path = tmp_path / "tasks.sqlite3"
        res_dir = tmp_path / "results"
        store = TaskStore(db_path, res_dir)
        store.initialize_sync()

        from remote_control.controller.confirmation import PendingConfirmation

        op = PendingConfirmation(
            operation_id="op-1",
            user_id=111222333,
            chat_id=111222333,
            workspace_alias="project",
            prompt="Hello from Termux",
            created_at=100.0,
            expires_at=200.0,
        )
        task_rec = await store.enqueue_task(op)

        queue = TaskQueue(
            task_store=store,
            executor=executor,
        )
        dispatcher.set_on_agent_online(queue.notify)
        queue.start()

        # Let worker loop cycle while executor is offline (is_agent_ready is False)
        await asyncio.sleep(0.1)
        r = await store.get_task(task_rec.id)
        assert r is not None and r.status == TaskStatus.QUEUED

        # Now mock agent check-in
        dispatcher._current_agent = AgentStatusInfo(
            agent_id="win-agent",
            hostname="PC",
            platform="Windows",
            cli_available=True,
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        dispatcher._last_heartbeat_monotonic = time.monotonic()

        # Fake dispatch_and_wait result
        async def fake_dispatch(*args: object, **kwargs: object) -> TaskResult:
            return TaskResult(
                task_id=task_rec.id,
                status=TaskStatus.SUCCESS,
                response="Remote agent executed task successfully.",
                duration_seconds=0.2,
            )

        dispatcher.dispatch_and_wait = AsyncMock(side_effect=fake_dispatch)  # type: ignore[method-assign]

        # Notify queue that agent is ready
        queue.notify()

        for _ in range(30):
            r_updated = await store.get_task(task_rec.id)
            if r_updated and r_updated.status == TaskStatus.SUCCESS:
                break
            await asyncio.sleep(0.05)

        await queue.stop()
        final_rec = await store.get_task(task_rec.id)
        assert final_rec is not None and final_rec.status == TaskStatus.SUCCESS

    asyncio.run(_test())
