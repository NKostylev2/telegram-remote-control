"""Tests for direct text message queueing, bottom reply keyboards, and menu commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.types import Chat, Message, User

from remote_control.agent.agy_client import AgyClient
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.config import AppConfig
from remote_control.controller.confirmation import ConfirmationStore
from remote_control.controller.handlers.run import (
    _format_task_result_message as run_format_result,
)
from remote_control.controller.handlers.run import (
    message_direct_prompt,
    set_user_active_workspace,
)
from remote_control.controller.keyboards import get_main_reply_keyboard
from remote_control.controller.main import setup_bot_commands
from remote_control.controller.task_queue import (
    _format_task_result_message as queue_format_result,
)
from remote_control.controller.task_store import TaskStore


def test_main_reply_keyboard_structure() -> None:
    """Test reply keyboard contains required 6 navigation buttons and resize/persistence flags."""
    kb = get_main_reply_keyboard()
    assert kb.resize_keyboard is True
    assert kb.is_persistent is not True

    button_texts = [btn.text for row in kb.keyboard for btn in row]
    assert "🚀 Запустить задачу" in button_texts
    assert "📸 Снимок экрана" in button_texts
    assert "📊 Статус" in button_texts
    assert "📋 Очередь задач" in button_texts
    assert "📁 Рабочая папка" in button_texts
    assert "❓ Помощь" in button_texts


def test_format_result_large_output_always_has_closed_pre() -> None:
    """Test that large CLI responses always produce well-formed HTML with closed <pre> tags."""
    large_response = "line 1: process abc\n" * 500  # ~10,000 characters
    res = TaskResult(
        task_id="11111111-1111-1111-1111-111111111111",
        status=TaskStatus.SUCCESS,
        response=large_response,
        duration_seconds=12.5,
    )

    # Test both formatters (queue and run)
    for formatter in (queue_format_result, run_format_result):
        if formatter == queue_format_result:
            formatted = formatter(res, "M:\\dev\\repo", 42)
        else:
            formatted = formatter(res, "M:\\dev\\repo")

        assert "<pre>" in formatted
        assert "</pre>" in formatted
        # Verify tag order and closure
        assert formatted.count("<pre>") == formatted.count("</pre>")
        # Length must be safe for Telegram
        assert len(formatted) <= 4096


def test_setup_bot_commands_registers_with_telegram() -> None:
    """Test setup_bot_commands registers bot commands via Bot.set_my_commands."""
    mock_bot = MagicMock(spec=Bot)
    mock_bot.set_my_commands = AsyncMock()

    asyncio.run(setup_bot_commands(mock_bot))
    mock_bot.set_my_commands.assert_awaited_once()
    commands, kwargs = mock_bot.set_my_commands.call_args
    registered_cmds = [cmd.command for cmd in commands[0]]
    assert "run" in registered_cmds
    assert "screenshot" in registered_cmds
    assert "tasks" in registered_cmds
    assert "status" in registered_cmds
    assert "cancel" in registered_cmds


def test_message_direct_prompt_initiates_confirmation(tmp_path: Path) -> None:
    """Test direct message in chat outside wizard initiates task confirmation."""
    async def _test() -> None:
        user_id = 111222333
        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()
        set_user_active_workspace(user_id, ws_dir)

        config = MagicMock(spec=AppConfig)
        config.allowed_workspaces = {"test_ws": ws_dir}

        mock_client = MagicMock(spec=AgyClient)
        mock_client.is_cli_available.return_value = True
        mock_client.interpret_prompt = AsyncMock(return_value="Понимание задачи: запуск процесса")

        store = ConfirmationStore()
        lock = asyncio.Lock()
        task_store = MagicMock(spec=TaskStore)

        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "Выдай список процессов"
        mock_msg.from_user = User(id=user_id, is_bot=False, first_name="Tester")
        mock_msg.chat = Chat(id=user_id, type="private")
        mock_msg.answer = AsyncMock()

        await message_direct_prompt(
            message=mock_msg,
            config=config,
            confirmation_store=store,
            agy_client=mock_client,
            task_lock=lock,
            task_store=task_store,
        )

        mock_msg.answer.assert_awaited_once()
        text = mock_msg.answer.call_args[0][0]
        assert "Выдай список процессов" in text
        assert len(store._operations) == 1
        mock_client.interpret_prompt.assert_not_awaited()

    asyncio.run(_test())


def test_message_direct_prompt_ignores_reply_buttons() -> None:
    """Test direct prompt handler ignores clicks on bottom reply keyboard buttons."""
    async def _test() -> None:
        config = MagicMock(spec=AppConfig)
        mock_client = MagicMock(spec=AgyClient)
        store = ConfirmationStore()
        lock = asyncio.Lock()

        mock_msg = MagicMock(spec=Message)
        mock_msg.text = "🚀 Запустить задачу"
        mock_msg.answer = AsyncMock()

        await message_direct_prompt(
            message=mock_msg,
            config=config,
            confirmation_store=store,
            agy_client=mock_client,
            task_lock=lock,
        )

        # Should do nothing and not answer
        mock_msg.answer.assert_not_awaited()
        assert len(store._operations) == 0

    asyncio.run(_test())
