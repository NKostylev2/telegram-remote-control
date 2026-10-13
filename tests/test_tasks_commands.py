"""Unit tests for /tasks, /cancel, and /result Telegram commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Chat, Message, User

from remote_control.agent.agy_client import AgyClient
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.controller.confirmation import ConfirmationStore
from remote_control.controller.handlers.run import callback_confirm_run
from remote_control.controller.handlers.tasks import (
    cb_cancel_task,
    cmd_cancel,
    cmd_result,
    cmd_tasks,
)
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
def mock_message() -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=111222, is_bot=False, first_name="Tester")
    msg.chat = Chat(id=111222, type="private")
    msg.answer = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


class TestTasksCommand:
    """Tests for /tasks command."""

    def test_tasks_empty_history(self, test_store: TaskStore, mock_message: MagicMock) -> None:
        """Req 16: Shows clear message when task history is empty."""
        async def _test() -> None:
            await cmd_tasks(mock_message, task_store=test_store)
            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]
            assert "История задач пуста" in text

        asyncio.run(_test())

    def test_tasks_displays_user_tasks_only(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 15 & 16: Displays only requesting user's tasks with status in Russian."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            # Enqueue task for user 111222
            op1 = PendingConfirmation(
                operation_id="op-u1",
                user_id=111222,
                chat_id=111222,
                workspace_alias="control",
                prompt="Check git status",
                created_at=100.0,
                expires_at=200.0,
            )
            t1 = await test_store.enqueue_task(op1)

            # Enqueue task for other user 999999
            op2 = PendingConfirmation(
                operation_id="op-u2",
                user_id=999999,
                chat_id=999999,
                workspace_alias="secret",
                prompt="Secret task",
                created_at=100.0,
                expires_at=200.0,
            )
            await test_store.enqueue_task(op2)

            await cmd_tasks(mock_message, task_store=test_store)
            text = mock_message.answer.call_args[0][0]

            assert f"#{t1.id}" in text
            assert "Check git status" in text
            assert "В очереди" in text
            # Other user's task MUST NOT be present
            assert "Secret task" not in text

        asyncio.run(_test())


class TestCancelCommand:
    """Tests for /cancel <id> command."""

    def test_cancel_requires_valid_id(self, mock_message: MagicMock) -> None:
        async def _test() -> None:
            queue = MagicMock(spec=TaskQueue)

            # Without arguments
            cmd_no_args = CommandObject(prefix="/", command="cancel", args=None)
            await cmd_cancel(mock_message, cmd_no_args, task_queue=queue)
            assert "Укажите ID задачи" in mock_message.answer.call_args[0][0]

            # With non-integer argument
            cmd_invalid = CommandObject(prefix="/", command="cancel", args="abc")
            await cmd_cancel(mock_message, cmd_invalid, task_queue=queue)
            assert "Неверный формат ID" in mock_message.answer.call_args[0][0]

        asyncio.run(_test())

    def test_cancel_other_user_task_rejected(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 15: Prohibits cancelling task belonging to another user."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-other",
                user_id=999999,  # other user
                chat_id=999999,
                workspace_alias="default",
                prompt="Build app",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)

            queue = TaskQueue(task_store=test_store, agy_client=MagicMock())
            cmd = CommandObject(prefix="/", command="cancel", args=str(task.id))
            await cmd_cancel(mock_message, cmd, task_queue=queue)

            text = mock_message.answer.call_args[0][0]
            assert "не являетесь владельцем" in text

        asyncio.run(_test())

    def test_cancel_queued_task(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 12: Cancels a queued task successfully."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-cancel-ok",
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="Task to cancel",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)

            queue = TaskQueue(task_store=test_store, agy_client=MagicMock())
            cmd = CommandObject(prefix="/", command="cancel", args=str(task.id))
            await cmd_cancel(mock_message, cmd, task_queue=queue)

            text = mock_message.answer.call_args[0][0]
            assert "отменена и удалена из очереди" in text

            # Verify in store
            updated = await test_store.get_task(task.id)
            assert updated is not None
            assert updated.status == TaskStatus.CANCELED

        asyncio.run(_test())


class TestResultCommand:
    """Tests for /result <id> command."""

    def test_result_for_running_task(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 17: Informs user if task is not finished yet."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-not-yet",
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="In progress",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)

            cmd = CommandObject(prefix="/", command="result", args=str(task.id))
            await cmd_result(mock_message, cmd, task_store=test_store)

            text = mock_message.answer.call_args[0][0]
            assert "ещё не завершена" in text

        asyncio.run(_test())

    def test_result_short_output_sent_as_text(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 17: Short results are sent in message text."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-short",
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="Short result prompt",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)
            await test_store.mark_task_running(task.id)

            res = TaskResult(
                task_id=uuid4(),
                status=TaskStatus.SUCCESS,
                response="Directory listing: file1.txt, file2.txt",
                duration_seconds=0.8,
            )
            await test_store.mark_task_finished(task.id, res)

            cmd = CommandObject(prefix="/", command="result", args=str(task.id))
            await cmd_result(mock_message, cmd, task_store=test_store)

            mock_message.answer.assert_awaited_once()
            text = mock_message.answer.call_args[0][0]
            assert "Directory listing: file1.txt" in text
            assert "0.8 сек." in text
            mock_message.answer_document.assert_not_awaited()

        asyncio.run(_test())

    def test_result_long_output_sent_as_document(
        self, test_store: TaskStore, mock_message: MagicMock
    ) -> None:
        """Req 17: Large outputs (>3000 chars) are attached as a file."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-long",
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="Long output prompt",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)
            await test_store.mark_task_running(task.id)

            long_text = "Line of code output\n" * 250
            result_file = test_store.save_result_file(task.id, long_text)

            res = TaskResult(
                task_id=uuid4(),
                status=TaskStatus.SUCCESS,
                response=long_text,
                duration_seconds=4.2,
            )
            await test_store.mark_task_finished(task.id, res, result_file_path=result_file)

            cmd = CommandObject(prefix="/", command="result", args=str(task.id))
            await cmd_result(mock_message, cmd, task_store=test_store)

            # Both message notification and document should be sent
            mock_message.answer.assert_awaited_once()
            mock_message.answer_document.assert_awaited_once()

            doc_call = mock_message.answer_document.call_args
            doc_arg = doc_call[0][0] if doc_call[0] else doc_call[1]["document"]
            assert doc_arg.filename == f"task_{task.id}_output.txt"

        asyncio.run(_test())


class TestConfirmationQueueIntegration:
    """Tests for confirmation flow enqueuing into SQLite."""

    def test_confirmation_enqueues_task_and_notifies_queue(
        self, test_store: TaskStore
    ) -> None:
        """Req 1 & 3: Confirmed task is stored in SQLite and notifies worker."""
        async def _test() -> None:
            conf_store = ConfirmationStore()
            op = conf_store.create_operation(
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="Deploy project",
            )

            mock_queue = MagicMock(spec=TaskQueue)
            mock_cb = MagicMock()
            mock_cb.data = f"cf:{op.operation_id}"
            mock_cb.from_user = User(id=111222, is_bot=False, first_name="Tester")
            mock_cb.message = MagicMock(spec=Message)
            mock_cb.message.chat = Chat(id=111222, type="private")
            mock_cb.message.edit_text = AsyncMock()
            mock_cb.answer = AsyncMock()

            await callback_confirm_run(
                callback=mock_cb,
                confirmation_store=conf_store,
                agy_client=MagicMock(spec=AgyClient),
                task_lock=asyncio.Lock(),
                task_store=test_store,
                task_queue=mock_queue,
            )

            # Verification:
            # 1. Enqueued in SQLite with ID 1
            rec = await test_store.get_task(1)
            assert rec is not None
            assert rec.operation_id == op.operation_id
            assert rec.prompt == "Deploy project"
            assert rec.status == TaskStatus.QUEUED

            # 2. Worker notified
            mock_queue.notify.assert_called_once()

            # 3. Message updated with task ID
            mock_cb.message.edit_text.assert_awaited_once()
            edited_text = mock_cb.message.edit_text.call_args[0][0]
            assert "Задача #1 добавлена в очередь!" in edited_text

        asyncio.run(_test())

    def test_cli_not_launched_if_db_enqueue_fails(
        self, test_store: TaskStore
    ) -> None:
        """Req 3: If database enqueuing fails, CLI is NEVER launched."""
        async def _test() -> None:
            conf_store = ConfirmationStore()
            op = conf_store.create_operation(
                user_id=111222,
                chat_id=111222,
                workspace_alias="default",
                prompt="Failing DB prompt",
            )

            broken_store = MagicMock(spec=TaskStore)
            broken_store.enqueue_task = AsyncMock(side_effect=RuntimeError("DB Disk Full"))

            mock_client = MagicMock(spec=AgyClient)
            mock_cb = MagicMock()
            mock_cb.data = f"cf:{op.operation_id}"
            mock_cb.from_user = User(id=111222, is_bot=False, first_name="Tester")
            mock_cb.message = MagicMock(spec=Message)
            mock_cb.message.chat = Chat(id=111222, type="private")
            mock_cb.answer = AsyncMock()

            await callback_confirm_run(
                callback=mock_cb,
                confirmation_store=conf_store,
                agy_client=mock_client,
                task_lock=asyncio.Lock(),
                task_store=broken_store,
                task_queue=MagicMock(),
            )

            # CLI MUST NOT be launched!
            mock_client.execute.assert_not_called()
            mock_cb.answer.assert_awaited_with(
                "Ошибка сохранения задачи в базу данных.", show_alert=True
            )

        asyncio.run(_test())


class TestTaskCancelCallback:
    """Tests for inline cancel button callback (task:cancel:<id>)."""

    def test_cancel_callback_success(self, test_store: TaskStore) -> None:
        """Owner clicks inline Cancel button -> task is cancelled."""
        async def _test() -> None:
            from remote_control.controller.confirmation import PendingConfirmation

            op = PendingConfirmation(
                operation_id="op-cb-cancel",
                user_id=111222,
                chat_id=111222,
                workspace_alias="control",
                prompt="Long running task",
                created_at=100.0,
                expires_at=200.0,
            )
            task = await test_store.enqueue_task(op)

            mock_queue = MagicMock(spec=TaskQueue)
            mock_queue.cancel_task = AsyncMock(
                return_value=(True, f"Задача #{task.id} отменена.", task)
            )

            mock_cb = MagicMock(spec=CallbackQuery)
            mock_cb.data = f"task:cancel:{task.id}"
            mock_cb.from_user = User(id=111222, is_bot=False, first_name="Tester")
            mock_msg = MagicMock(spec=Message)
            mock_msg.edit_text = AsyncMock()
            mock_cb.message = mock_msg
            mock_cb.answer = AsyncMock()

            await cb_cancel_task(mock_cb, task_queue=mock_queue)

            mock_queue.cancel_task.assert_awaited_once_with(task_id=task.id, user_id=111222)
            mock_cb.answer.assert_awaited_once_with("Запрос на отмену отправлен!", show_alert=False)
            mock_msg.edit_text.assert_awaited_once()
            assert f"Задача #{task.id} отменяется" in mock_msg.edit_text.call_args[0][0]

        asyncio.run(_test())

    def test_cancel_callback_permission_denied(self, test_store: TaskStore) -> None:
        """Other user clicks inline Cancel button -> rejected with alert."""
        async def _test() -> None:
            mock_queue = MagicMock(spec=TaskQueue)
            mock_queue.cancel_task = AsyncMock(
                return_value=(False, "Вы не являетесь владельцем задачи #1.", None)
            )

            mock_cb = MagicMock(spec=CallbackQuery)
            mock_cb.data = "task:cancel:1"
            mock_cb.from_user = User(id=999999, is_bot=False, first_name="Intruder")
            mock_cb.answer = AsyncMock()

            await cb_cancel_task(mock_cb, task_queue=mock_queue)

            mock_queue.cancel_task.assert_awaited_once_with(task_id=1, user_id=999999)
            mock_cb.answer.assert_awaited_once_with(
                "Вы не являетесь владельцем задачи #1.", show_alert=True
            )

        asyncio.run(_test())

    def test_cancel_callback_invalid_id(self) -> None:
        """Malformed callback data is safely handled."""
        async def _test() -> None:
            mock_queue = MagicMock(spec=TaskQueue)
            mock_cb = MagicMock(spec=CallbackQuery)
            mock_cb.data = "task:cancel:not_a_number"
            mock_cb.from_user = User(id=111222, is_bot=False, first_name="Tester")
            mock_cb.answer = AsyncMock()

            await cb_cancel_task(mock_cb, task_queue=mock_queue)

            mock_cb.answer.assert_awaited_once_with(
                "Неверный идентификатор задачи.", show_alert=True
            )
            mock_queue.cancel_task.assert_not_called()

        asyncio.run(_test())
