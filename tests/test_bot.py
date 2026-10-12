"""Unit tests for Telegram Bot controller, authorization, commands, and confirmation.

All tests are completely offline and use mocks for Telegram API and AgyClient.
Does NOT require a real .env file, network connection, or Telegram bot token.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Chat, Message, User

from remote_control.agent.agy_client import AgyClient
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.config import AppConfig
from remote_control.controller.bot import create_bot, create_dispatcher
from remote_control.controller.confirmation import ConfirmationStatus, ConfirmationStore
from remote_control.controller.handlers.base import (
    cmd_help,
    cmd_start,
    cmd_status,
    cmd_wake,
    cmd_workspace,
)
from remote_control.controller.handlers.run import (
    RunWizard,
    _build_post_execution_keyboard,
    _format_task_result_message,
    callback_cancel_run,
    callback_confirm_run,
    callback_toggle_effort,
    cb_post_execution_change_folder,
    cb_post_execution_same_folder,
    cb_wizard_cancel,
    cb_wizard_change_page,
    cb_wizard_home,
    cb_wizard_open_folder,
    cb_wizard_select_current_folder,
    cb_wizard_select_drive,
    cb_wizard_select_workspace,
    cb_wizard_show_drives,
    cmd_run,
    message_received_prompt,
)
from remote_control.controller.main import run_bot
from remote_control.controller.middlewares import AuthorizationMiddleware


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
    """Create a test AppConfig fixture."""
    ws = tmp_path / "test_workspace"
    ws.mkdir(parents=True, exist_ok=True)
    fake_cli = tmp_path / "fake_agy.exe"
    fake_cli.write_text("binary", encoding="utf-8")

    return AppConfig(
        TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
        ALLOWED_TELEGRAM_USERS=[111222333, 444555666],
        ALLOWED_WORKSPACES={"project1": str(ws)},
        ANTIGRAVITY_CLI_PATH=str(fake_cli),
        TASK_EXECUTION_TIMEOUT_SECONDS=60,
    )


@pytest.fixture
def mock_message() -> MagicMock:
    """Create a mock Telegram Message for testing."""
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=111222333, is_bot=False, first_name="Kostya")
    msg.chat = Chat(id=111222333, type=ChatType.PRIVATE)
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


@pytest.fixture
def mock_callback(mock_message: MagicMock) -> MagicMock:
    """Create a mock CallbackQuery for testing."""
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=111222333, is_bot=False, first_name="Kostya")
    cb.message = mock_message
    cb.answer = AsyncMock()
    return cb


class TestAuthorizationMiddleware:
    """Tests for user authorization, chat type check, and stealth mode."""

    def test_authorized_user_allowed(self, test_config: AppConfig, mock_message: MagicMock) -> None:
        async def _test() -> None:
            mw = AuthorizationMiddleware(test_config)
            handler = AsyncMock(return_value="OK")
            result = await mw(handler, mock_message, {})
            assert result == "OK"
            handler.assert_awaited_once()

        asyncio.run(_test())

    def test_unauthorized_user_stealth_dropped(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            mock_message.from_user = User(id=999999999, is_bot=False, first_name="Stranger")
            mw = AuthorizationMiddleware(test_config)
            handler = AsyncMock()

            result = await mw(handler, mock_message, {})
            assert result is None
            handler.assert_not_awaited()
            mock_message.answer.assert_not_awaited()

        asyncio.run(_test())

    def test_empty_allowlist_forbids_all(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            config = AppConfig(
                TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
                ALLOWED_TELEGRAM_USERS=[],
            )
            mw = AuthorizationMiddleware(config)
            handler = AsyncMock()

            result = await mw(handler, mock_message, {})
            assert result is None
            handler.assert_not_awaited()

        asyncio.run(_test())

    def test_group_chat_rejected(self, test_config: AppConfig, mock_message: MagicMock) -> None:
        async def _test() -> None:
            mock_message.chat = Chat(id=-100123456789, type=ChatType.GROUP)
            mw = AuthorizationMiddleware(test_config)
            handler = AsyncMock()

            result = await mw(handler, mock_message, {})
            assert result is None
            handler.assert_not_awaited()

        asyncio.run(_test())

    def test_callback_query_authorized(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            mw = AuthorizationMiddleware(test_config)
            handler = AsyncMock(return_value="OK")

            result = await mw(handler, mock_callback, {})
            assert result == "OK"
            handler.assert_awaited_once()

        asyncio.run(_test())

    def test_callback_query_unauthorized_dropped(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            mock_callback.from_user = User(id=888888888, is_bot=False, first_name="Intruder")
            mw = AuthorizationMiddleware(test_config)
            handler = AsyncMock()

            result = await mw(handler, mock_callback, {})
            assert result is None
            handler.assert_not_awaited()
            mock_callback.answer.assert_not_awaited()

        asyncio.run(_test())


class TestConfirmationStore:
    """Tests for in-memory confirmation storage and race-condition safety."""

    def test_create_and_confirm_operation(self) -> None:
        store = ConfirmationStore(default_ttl_seconds=120)
        op = store.create_operation(
            user_id=111,
            chat_id=111,
            workspace_alias="project1",
            prompt="Do task",
        )

        assert op.status == ConfirmationStatus.PENDING
        assert not op.is_expired

        success, err, claimed_op = store.confirm_operation(op.operation_id, 111, 111)
        assert success is True
        assert err == ""
        assert claimed_op is not None
        assert claimed_op.status == ConfirmationStatus.EXECUTING

        # Repeated confirmation must be rejected (single-execution protection)
        success2, err2, _ = store.confirm_operation(op.operation_id, 111, 111)
        assert success2 is False
        assert "уже обработана" in err2

    def test_confirm_by_different_user_rejected(self) -> None:
        store = ConfirmationStore()
        op = store.create_operation(user_id=111, chat_id=111, workspace_alias="p1", prompt="x")

        success, err, _ = store.confirm_operation(op.operation_id, user_id=222, chat_id=111)
        assert success is False
        assert "не являетесь инициатором" in err

    def test_confirm_expired_rejected(self) -> None:
        store = ConfirmationStore(default_ttl_seconds=-1)  # immediate expire
        op = store.create_operation(user_id=111, chat_id=111, workspace_alias="p1", prompt="x")

        success, err, _ = store.confirm_operation(op.operation_id, 111, 111)
        assert success is False
        assert "истёк" in err

    def test_cancel_operation(self) -> None:
        store = ConfirmationStore()
        op = store.create_operation(user_id=111, chat_id=111, workspace_alias="p1", prompt="x")

        success, err, cancelled_op = store.cancel_operation(op.operation_id, 111, 111)
        assert success is True
        assert cancelled_op is not None
        assert cancelled_op.status == ConfirmationStatus.CANCELLED

        # Cannot confirm after cancellation
        success2, _, _ = store.confirm_operation(op.operation_id, 111, 111)
        assert success2 is False


class TestBaseCommands:
    """Tests for informational bot commands."""

    def test_start_command(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            await cmd_start(mock_message)
            mock_message.answer.assert_awaited_once()
            args, _ = mock_message.answer.call_args
            assert "Telegram Remote Control" in args[0]
            assert "/help" in args[0]

        asyncio.run(_test())

    def test_help_command_lists_all_commands(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            await cmd_help(mock_message)
            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]
            assert "/run" in text
            assert "/status" in text
            assert "/workspace" in text
            assert "/wake" in text
            assert "/tasks" in text
            assert "/cancel" in text
            assert "/screenshot" in text
            assert "/result" in text

        asyncio.run(_test())

    def test_status_command(self, test_config: AppConfig, mock_message: MagicMock) -> None:
        async def _test() -> None:
            mock_client = MagicMock(spec=AgyClient)
            mock_client.is_cli_available.return_value = True

            await cmd_status(mock_message, test_config, mock_client)
            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]

            assert "Локальный Controller" in text
            assert "🟢 Доступен" in text
            assert "2 пользователей" in text
            assert "1" in text  # 1 workspace
            # Confirm secrets are masked
            assert test_config.telegram_bot_token.get_secret_value() not in text

        asyncio.run(_test())

    def test_workspace_command_lists_aliases_without_paths(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            await cmd_workspace(mock_message, test_config)
            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]

            assert "project1" in text
            assert "🟢 Доступно" in text
            # Must NOT expose full filesystem path
            configured_path = str(test_config.allowed_workspaces["project1"])
            assert configured_path not in text

        asyncio.run(_test())

    def test_unimplemented_command_placeholders(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            await cmd_wake(mock_message)
            assert "Wake-on-LAN" in mock_message.answer.call_args[0][0]

        asyncio.run(_test())


class TestRunCommandAndConfirmation:
    """Tests for /run command validation, interactive wizard, confirmation flow, and execution."""

    def test_run_missing_args_starts_wizard(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            store = ConfirmationStore()
            lock = asyncio.Lock()
            cmd = CommandObject(prefix="/", command="run", args=None)
            mock_state = AsyncMock(spec=FSMContext)

            await cmd_run(mock_message, cmd, test_config, client, store, lock, state=mock_state)

            mock_state.set_state.assert_awaited_once_with(RunWizard.choosing_directory)
            mock_message.answer.assert_awaited_once()
            assert "Выберите рабочую директорию" in mock_message.answer.call_args[0][0]
            assert len(store._operations) == 0

        asyncio.run(_test())

    def test_run_single_arg_shows_help(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            store = ConfirmationStore()
            lock = asyncio.Lock()
            cmd = CommandObject(prefix="/", command="run", args="project1")
            mock_state = AsyncMock(spec=FSMContext)

            await cmd_run(mock_message, cmd, test_config, client, store, lock, state=mock_state)

            mock_message.answer.assert_awaited_once()
            assert "Укажите текст задачи" in mock_message.answer.call_args[0][0]
            assert len(store._operations) == 0

        asyncio.run(_test())

    def test_run_unknown_workspace(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            store = ConfirmationStore()
            lock = asyncio.Lock()
            cmd = CommandObject(prefix="/", command="run", args="unknown_ws Do work")
            mock_state = AsyncMock(spec=FSMContext)

            await cmd_run(mock_message, cmd, test_config, client, store, lock, state=mock_state)
            mock_message.answer.assert_awaited_once()
            assert "не найдено в списке сохранённых" in mock_message.answer.call_args[0][0]
            assert len(store._operations) == 0

        asyncio.run(_test())

    def test_run_asks_for_confirmation_without_invoking_cli(
        self, test_config: AppConfig, mock_message: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            client.is_cli_available.return_value = True
            client.resolve_workspace.return_value = Path("valid")
            client.execute = AsyncMock()
            client.interpret_prompt = AsyncMock(
                return_value="План: выполнить тестовую задачу."
            )

            store = ConfirmationStore()
            lock = asyncio.Lock()
            cmd = CommandObject(prefix="/", command="run", args="project1 Test prompt")
            mock_state = AsyncMock(spec=FSMContext)

            await cmd_run(mock_message, cmd, test_config, client, store, lock, state=mock_state)

            mock_message.answer.assert_awaited_once()
            text, kwargs = mock_message.answer.call_args[0][0], mock_message.answer.call_args[1]

            assert "Правильно ли агент понял задачу?" in text
            assert "project1" in text
            assert "Test prompt" in text
            assert "reply_markup" in kwargs

            # CLI MUST NOT be called yet
            client.execute.assert_not_awaited()
            client.interpret_prompt.assert_not_awaited()
            # Operation registered in memory
            assert len(store._operations) == 1

        asyncio.run(_test())

    def test_callback_confirm_launches_cli(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            client.execute = AsyncMock(
                return_value=TaskResult(
                    task_id=uuid4(),
                    status=TaskStatus.SUCCESS,
                    response="Task finished.",
                    duration_seconds=3.2,
                )
            )
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="project1",
                prompt="Fix bug",
            )
            lock = asyncio.Lock()

            mock_callback.data = f"cf:{op.operation_id}"
            await callback_confirm_run(mock_callback, store, client, lock)

            # Check CLI was executed
            client.execute.assert_awaited_once_with(
                prompt="Fix bug",
                workspace_alias="project1",
                workspace_path=None,
                effort="low",
            )
            # Operation marked completed
            assert store.get_operation(op.operation_id).status == ConfirmationStatus.COMPLETED

            # Result delivered to chat
            mock_callback.message.answer.assert_awaited_once()
            result_text = mock_callback.message.answer.call_args[0][0]
            assert "Задача выполнена" in result_text
            assert "Task finished." in result_text

        asyncio.run(_test())

    def test_callback_confirm_run_sends_document_for_large_output(
        self, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            large_text = "Line of output\n" * 300
            client.execute = AsyncMock(
                return_value=TaskResult(
                    task_id=uuid4(),
                    status=TaskStatus.SUCCESS,
                    response=large_text,
                    exit_code=0,
                    duration_seconds=2.5,
                )
            )
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="project1",
                prompt="List all processes",
            )
            lock = asyncio.Lock()

            mock_callback.data = f"cf:{op.operation_id}"
            await callback_confirm_run(mock_callback, store, client, lock)

            mock_callback.message.answer.assert_awaited_once()
            mock_callback.message.answer_document.assert_awaited_once()
            doc_arg = mock_callback.message.answer_document.call_args[0][0]
            assert doc_arg.filename == "task_output.txt"

        asyncio.run(_test())

    def test_callback_cancel_aborts_without_cli(
        self, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="project1",
                prompt="Fix bug",
            )
            mock_callback.data = f"cl:{op.operation_id}"

            await callback_cancel_run(mock_callback, store)

            assert store.get_operation(op.operation_id).status == ConfirmationStatus.CANCELLED
            mock_callback.message.edit_text.assert_awaited_once()
            assert "отменён" in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_callback_toggle_effort_cycles_levels(
        self, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="project1",
                prompt="Launch app",
                effort="low",
            )
            mock_callback.data = f"ef:{op.operation_id}"

            # low -> medium
            await callback_toggle_effort(mock_callback, store)
            assert store.get_operation(op.operation_id).effort == "medium"
            mock_callback.answer.assert_awaited_with("Режим: MEDIUM")

            # medium -> high
            await callback_toggle_effort(mock_callback, store)
            assert store.get_operation(op.operation_id).effort == "high"
            mock_callback.answer.assert_awaited_with("Режим: HIGH")

            # high -> low
            await callback_toggle_effort(mock_callback, store)
            assert store.get_operation(op.operation_id).effort == "low"
            mock_callback.answer.assert_awaited_with("Режим: LOW")

        asyncio.run(_test())


    def test_concurrency_lock_blocks_second_task(
        self, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="project1",
                prompt="Task 2",
            )
            lock = asyncio.Lock()
            await lock.acquire()  # simulate busy host

            mock_callback.data = f"cf:{op.operation_id}"
            await callback_confirm_run(mock_callback, store, client, lock)

            # Blocked
            mock_callback.answer.assert_awaited_once()
            assert "занят" in mock_callback.answer.call_args[0][0]
            client.execute.assert_not_awaited()

        asyncio.run(_test())

    def test_post_execution_keyboard_builder(self) -> None:
        kb = _build_post_execution_keyboard("op-1234")
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[0][0].callback_data == "post:same:op-1234"
        assert "Новый запрос" in kb.inline_keyboard[0][0].text
        assert kb.inline_keyboard[1][0].callback_data == "post:change"
        assert "Сменить директорию" in kb.inline_keyboard[1][0].text

    def test_post_execution_same_folder_sets_prompt_state(
        self, test_config: AppConfig, mock_callback: MagicMock, tmp_path: Path
    ) -> None:
        async def _test() -> None:
            store = ConfirmationStore()
            op = store.create_operation(
                user_id=111222333,
                chat_id=111222333,
                workspace_alias="custom",
                prompt="Initial task",
                workspace_path=tmp_path,
            )
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = f"post:same:{op.operation_id}"

            await cb_post_execution_same_folder(
                mock_callback, mock_state, store, test_config
            )

            mock_state.set_state.assert_awaited_once_with(RunWizard.waiting_for_prompt)
            mock_state.update_data.assert_awaited_once_with(
                selected_path=str(tmp_path),
                workspace_alias="custom",
            )
            mock_callback.message.answer.assert_awaited_once()
            ans_text = mock_callback.message.answer.call_args[0][0]
            assert "Рабочая директория сохранена" in ans_text
            assert str(tmp_path) in ans_text

        asyncio.run(_test())

    def test_post_execution_same_folder_expired_session(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            store = ConfirmationStore()
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "post:same:nonexistent-op"

            await cb_post_execution_same_folder(
                mock_callback, mock_state, store, test_config
            )

            mock_callback.answer.assert_awaited_once()
            assert "устарела" in mock_callback.answer.call_args[0][0]
            mock_state.set_state.assert_not_awaited()

        asyncio.run(_test())

    def test_post_execution_change_folder_opens_wizard(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "post:change"

            await cb_post_execution_change_folder(
                mock_callback, mock_state, test_config
            )

            mock_state.clear.assert_awaited_once()
            mock_state.set_state.assert_awaited_once_with(RunWizard.choosing_directory)
            mock_callback.message.answer.assert_awaited_once()
            ans_text = mock_callback.message.answer.call_args[0][0]
            assert "Выберите рабочую директорию" in ans_text

        asyncio.run(_test())

    def test_wizard_select_workspace(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "wz:ws:project1"

            await cb_wizard_select_workspace(mock_callback, test_config, mock_state)

            mock_state.set_state.assert_awaited_once_with(RunWizard.waiting_for_prompt)
            mock_state.update_data.assert_awaited_once()
            mock_callback.message.edit_text.assert_awaited_once()
            edit_text = mock_callback.message.edit_text.call_args[0][0]
            assert "Выбрана рабочая директория" in edit_text
            assert "project1" in edit_text

        asyncio.run(_test())

    def test_wizard_show_drives(self, mock_callback: MagicMock) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "wz:drives"

            await cb_wizard_show_drives(mock_callback, mock_state)

            mock_state.set_state.assert_awaited_once_with(RunWizard.choosing_directory)
            mock_callback.message.edit_text.assert_awaited_once()
            edit_text = mock_callback.message.edit_text.call_args[0][0]
            assert "Выберите системный диск" in edit_text

        asyncio.run(_test())

    def test_wizard_home_returns_to_initial(
        self, test_config: AppConfig, mock_callback: MagicMock
    ) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "wz:home"

            await cb_wizard_home(mock_callback, test_config, mock_state)

            mock_state.set_state.assert_awaited_once_with(RunWizard.choosing_directory)
            mock_callback.message.edit_text.assert_awaited_once()
            assert "Выберите рабочую директорию" in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_wizard_cancel(self, mock_callback: MagicMock) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "wz:cancel"

            await cb_wizard_cancel(mock_callback, mock_state)

            mock_state.clear.assert_awaited_once()
            mock_callback.message.edit_text.assert_awaited_once()
            assert "отменён" in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_wizard_message_prompt_creates_confirmation(
        self, mock_message: MagicMock, tmp_path: Path
    ) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            client.is_cli_available.return_value = True
            store = ConfirmationStore()
            lock = asyncio.Lock()

            mock_state = AsyncMock(spec=FSMContext)
            mock_state.get_data.return_value = {
                "selected_path": str(tmp_path),
                "workspace_alias": "tmp_ws",
            }
            client.interpret_prompt = AsyncMock(
                return_value="План: создать скрипт приветствия."
            )
            mock_message.text = "Создай скрипт приветствия"

            await message_received_prompt(
                mock_message,
                mock_state,
                store,
                client,
                lock,
            )

            mock_state.clear.assert_awaited_once()
            assert len(store._operations) == 1
            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]
            assert "Правильно ли агент понял задачу?" in text
            assert "Создай скрипт приветствия" in text
            assert str(tmp_path) in text
            client.interpret_prompt.assert_not_awaited()

        asyncio.run(_test())

    def test_wizard_message_prompt_cancel_cmd(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            client = MagicMock(spec=AgyClient)
            store = ConfirmationStore()
            lock = asyncio.Lock()
            mock_state = AsyncMock(spec=FSMContext)
            mock_message.text = "/cancel"

            await message_received_prompt(
                mock_message,
                mock_state,
                store,
                client,
                lock,
            )

            mock_state.clear.assert_awaited_once()
            mock_message.answer.assert_awaited_once()
            assert "отменён" in mock_message.answer.call_args[0][0]
            assert len(store._operations) == 0

        asyncio.run(_test())

    def test_wizard_select_drive(self, mock_callback: MagicMock) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_callback.data = "wz:drv:C"

            await cb_wizard_select_drive(mock_callback, mock_state)

            mock_state.update_data.assert_awaited_once()
            mock_callback.message.edit_text.assert_awaited_once()
            assert "Текущий путь" in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_wizard_open_folder(self, mock_callback: MagicMock, tmp_path: Path) -> None:
        async def _test() -> None:
            sub = tmp_path / "subfolder"
            sub.mkdir()
            mock_state = AsyncMock(spec=FSMContext)
            mock_state.get_data.return_value = {
                "subdirs": [str(sub)],
                "current_path": str(tmp_path),
            }
            mock_callback.data = "wz:dir:0"

            await cb_wizard_open_folder(mock_callback, mock_state)

            mock_state.update_data.assert_awaited_once()
            mock_callback.message.edit_text.assert_awaited_once()
            assert str(sub) in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_wizard_change_page(self, mock_callback: MagicMock, tmp_path: Path) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_state.get_data.return_value = {
                "current_path": str(tmp_path),
                "subdirs": [str(tmp_path / f"folder_{i}") for i in range(10)],
            }
            mock_callback.data = "wz:page:1"

            await cb_wizard_change_page(mock_callback, mock_state)

            mock_state.update_data.assert_awaited_once_with(page=1)
            mock_callback.message.edit_text.assert_awaited_once()

        asyncio.run(_test())

    def test_wizard_select_current_folder(
        self, mock_callback: MagicMock, tmp_path: Path
    ) -> None:
        async def _test() -> None:
            mock_state = AsyncMock(spec=FSMContext)
            mock_state.get_data.return_value = {
                "current_path": str(tmp_path),
            }
            mock_callback.data = "wz:select"

            await cb_wizard_select_current_folder(mock_callback, mock_state)

            mock_state.set_state.assert_awaited_once_with(RunWizard.waiting_for_prompt)
            mock_callback.message.edit_text.assert_awaited_once()
            assert str(tmp_path) in mock_callback.message.edit_text.call_args[0][0]

        asyncio.run(_test())


class TestResultFormattingAndEdgeCases:
    """Tests for task result text formatting, soft-denied tools, and truncation."""

    def test_partial_success_formatting(self) -> None:
        res = TaskResult(
            task_id=uuid4(),
            status=TaskStatus.PARTIAL_SUCCESS,
            response="Code analyzed.",
            duration_seconds=5.0,
            soft_denied_tools=["file_write", "terminal_exec"],
            warnings=["Tool permission denied by sandbox policy"],
        )
        text = _format_task_result_message(res, "project1")
        assert "Задача выполнена с ограничениями" in text
        assert "file_write" in text
        assert "terminal_exec" in text
        assert "Code analyzed." in text

    def test_long_response_truncated(self) -> None:
        very_long_text = "A" * 6000
        res = TaskResult(
            task_id=uuid4(),
            status=TaskStatus.SUCCESS,
            response=very_long_text,
            duration_seconds=1.0,
        )
        text = _format_task_result_message(res, "project1")
        assert len(text) <= 4000
        assert "вывод обрезан по лимиту Telegram" in text


class TestBotCreationAndStartupFailures:
    """Tests for bot initialization, dispatcher creation, and startup failure handling."""

    def test_create_bot_missing_token_raises(self) -> None:
        config = AppConfig(TELEGRAM_BOT_TOKEN=None)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is missing"):
            create_bot(config)

    def test_create_bot_with_proxy(self) -> None:
        config = AppConfig(
            TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
            TELEGRAM_PROXY="http://127.0.0.1:1372",
        )
        bot = create_bot(config)
        assert bot.session is not None

    def test_create_dispatcher_registers_routes(self, test_config: AppConfig) -> None:
        dp = create_dispatcher(test_config)
        assert dp["config"] == test_config
        assert isinstance(dp["confirmation_store"], ConfirmationStore)

    def test_run_bot_aborts_without_token(self) -> None:
        async def _test() -> None:
            config = AppConfig(TELEGRAM_BOT_TOKEN=None)
            with pytest.raises(SystemExit) as exc:
                await run_bot(config)
            assert exc.value.code == 1

        asyncio.run(_test())

    def test_no_secrets_in_logs(
        self, test_config: AppConfig, mock_message: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def _test() -> None:
            caplog.set_level(logging.DEBUG)
            mw = AuthorizationMiddleware(test_config)
            await mw(AsyncMock(return_value="OK"), mock_message, {})

            # Verify token secret is never in captured logs
            raw_token = test_config.telegram_bot_token.get_secret_value()
            for record in caplog.records:
                assert raw_token not in record.getMessage()

        asyncio.run(_test())
