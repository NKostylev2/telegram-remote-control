"""Sequential background task queue worker and lifecycle executor."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from remote_control.agent.agy_client import AgyClient
from remote_control.common.executor import LocalTaskExecutor, TaskExecutor
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.config import AppConfig
from remote_control.controller.keyboards import build_task_cancel_keyboard
from remote_control.controller.task_store import TaskRecord, TaskStore

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_MESSAGE_LENGTH = 4000


def _build_post_execution_keyboard(operation_id: str) -> InlineKeyboardMarkup:
    """Build follow-up actions keyboard after task completion."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Новый запрос в этой папке",
                    callback_data=f"post:same:{operation_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📁 Сменить директорию",
                    callback_data="post:change",
                ),
            ],
        ]
    )


def _format_task_result_message(result: TaskResult, workspace_display: str, task_id: int) -> str:
    """Format TaskResult safely into an informative Telegram HTML message."""
    status_header = {
        TaskStatus.SUCCESS: f"✅ <b>Задача #{task_id} выполнена</b>",
        TaskStatus.PARTIAL_SUCCESS: f"⚠️ <b>Задача #{task_id} выполнена с ограничениями</b>",
        TaskStatus.ERROR: f"❌ <b>Ошибка при выполнении задачи #{task_id}</b>",
        TaskStatus.CANCELED: f"⚪ <b>Задача #{task_id} отменена</b>",
        TaskStatus.INTERRUPTED: f"⏱️ <b>Выполнение задачи #{task_id} прервано</b>",
    }.get(result.status, f"ℹ️ <b>Задача #{task_id}: Статус {result.status}</b>")

    lines = [
        status_header,
        f"📁 Директория: <code>{html.escape(workspace_display)}</code>",
        f"⏱️ Длительность: {result.duration_seconds:.1f} сек.",
    ]

    if result.conversation_id:
        lines.append(f"🆔 Conversation ID: <code>{html.escape(result.conversation_id)}</code>")

    if result.soft_denied_tools:
        tools_str = ", ".join(result.soft_denied_tools)
        lines.append(
            f"⚠️ <i>Отклонённые инструменты (soft-denied):</i> "
            f"<code>{html.escape(tools_str)}</code>"
        )

    if result.warnings:
        warn_str = "; ".join(result.warnings[:3])
        lines.append(f"⚠️ <i>Предупреждения:</i> {html.escape(warn_str)}")

    if result.error:
        err_snippet = result.error[:500]
        lines.append(f"\n❌ <b>Ошибка:</b>\n<pre>{html.escape(err_snippet)}</pre>")

    if result.response and result.response.strip():
        resp_text = result.response.strip()
        lines.append("\n📝 <b>Ответ модели:</b>")
        # Budget max 2500 characters for snippet so whole message is safely <= 4000 characters
        if len(resp_text) > 2500:
            snippet = resp_text[:2500]
            lines.append(f"<pre>{html.escape(snippet)}</pre>")
            lines.append(
                "<i>[... вывод обрезан по лимиту Telegram, полный файл прикреплён]</i>"
            )
        else:
            lines.append(f"<pre>{html.escape(resp_text)}</pre>")
    elif not result.error:
        lines.append(
            "\n📝 <b>Результат:</b> "
            "<i>Действие выполнено на хосте (текстовый вывод отсутствует).</i>"
        )

    return "\n".join(lines)


class TaskQueue:
    """Sequential background task worker ensuring single-task execution from SQLite."""

    def __init__(
        self,
        task_store: TaskStore,
        agy_client: AgyClient | None = None,
        bot: Bot | None = None,
        executor: TaskExecutor | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.task_store = task_store
        self.agy_client = agy_client
        self.bot = bot
        self.config = config

        if executor is not None:
            self.executor = executor
        elif agy_client is not None:
            self.executor = LocalTaskExecutor(agy_client)
        else:
            raise ValueError("Either executor or agy_client must be provided to TaskQueue.")

        self._work_available = asyncio.Event()
        self._running = False
        self._worker_task: asyncio.Task[None] | None = None

        # Active task tracking for cancellation and concurrency safety
        self._current_task_record: TaskRecord | None = None
        self._current_exec_task: asyncio.Task[TaskResult] | None = None
        self._cancel_lock = asyncio.Lock()
        self._pending_bg_tasks: set[asyncio.Task[Any]] = set()

    def set_bot(self, bot: Bot) -> None:
        """Inject or update Bot instance."""
        self.bot = bot

    def notify(self) -> None:
        """Signal the background worker that new work has been enqueued."""
        self._work_available.set()

    def start(self) -> None:
        """Start the background queue worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop(), name="task_queue_worker")
        logger.info("Task queue worker started.")

    async def stop(self) -> None:
        """Stop the background queue worker and await its termination."""
        if not self._running:
            return
        logger.info("Stopping task queue worker...")
        self._running = False
        self._work_available.set()

        # If a task is currently executing, cancel it
        if self._current_exec_task and not self._current_exec_task.done():
            self._current_exec_task.cancel()

        # Cancel and await all pending background notification tasks
        if self._pending_bg_tasks:
            for t in list(self._pending_bg_tasks):
                t.cancel()
            await asyncio.gather(*self._pending_bg_tasks, return_exceptions=True)
            self._pending_bg_tasks.clear()

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        logger.info("Task queue worker stopped.")

    async def _worker_loop(self) -> None:
        """Main loop: sequentially processes tasks from SQLite."""
        while self._running:
            try:
                # 1. Clear work signal BEFORE checking DB to eliminate lost wakeup race condition
                self._work_available.clear()

                # 2. Look for next queued task in database
                task = await self.task_store.get_next_queued_task()
                if task is None:
                    # Wait for new work signal or periodic check
                    try:
                        await asyncio.wait_for(self._work_available.wait(), timeout=5.0)
                    except TimeoutError:
                        pass
                    continue

                # If executor is temporarily unavailable (e.g. Windows Agent offline in
                # distributed mode), leave task in QUEUED and wait for readiness.
                if not self.executor.is_available():
                    logger.debug(
                        "Task #%d waiting in queue: executor is not available yet.", task.id
                    )
                    try:
                        await asyncio.wait_for(self._work_available.wait(), timeout=5.0)
                    except TimeoutError:
                        pass
                    continue

                # 3. Process this task
                await self._process_single_task(task)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Unexpected error in task queue worker loop: %s", e)
                await asyncio.sleep(2.0)

    async def _send_start_notification(
        self, task: TaskRecord, display_dir: str
    ) -> Any | None:
        """Asynchronously send start notification to Telegram without blocking CLI execution."""
        if self.bot is None:
            return None
        try:
            return await self.bot.send_message(
                chat_id=task.chat_id,
                text=(
                    f"🚀 <b>Задача #{task.id} запущена на выполнение!</b>\n\n"
                    f"📁 Директория: <code>{html.escape(display_dir)}</code>\n"
                    f"⚙️ Режим рассуждения: <code>{html.escape(task.effort.upper())}</code>\n\n"
                    "<i>Выполняется вызов Antigravity CLI, ожидайте результат...</i>"
                ),
                reply_markup=build_task_cancel_keyboard(task.id),
                parse_mode="HTML",
            )
        except TelegramAPIError as e:
            logger.warning("Failed sending start notification for Task #%d: %s", task.id, e)
            return None
        except Exception as e:
            logger.debug("Non-Telegram error sending start notification: %s", e)
            return None

    async def _run_task_with_progress_notifications(
        self,
        task: TaskRecord,
        display_dir: str,
        tracker: list[Any | None],
    ) -> None:
        """Send start notification and then periodically verify agent health.

        Replaces status messages with updated progress while task is in progress.
        """
        if self.bot is None:
            return

        # 1. Start notification
        start_msg = await self._send_start_notification(task, display_dir)
        tracker[0] = getattr(start_msg, "message_id", None) if start_msg is not None else None

        # 2. Periodic loop
        config = self.config or getattr(getattr(self, "agy_client", None), "config", None)
        interval = getattr(config, "task_progress_interval_seconds", 60) if config else 60
        start_mono = time.monotonic()

        while self._running:
            await asyncio.sleep(interval)

            if self._current_task_record is None or self._current_task_record.id != task.id:
                break

            # Assess executor health
            if not self.executor.is_available():
                status_desc = "⚠️ <b>Windows Agent не отвечает / оффлайн</b>"
            else:
                status_desc = "🟢 Агент активно выполняет задачу..."

            elapsed_secs = int(time.monotonic() - start_mono)
            mins = elapsed_secs // 60
            secs = elapsed_secs % 60
            dur_str = f"{mins} мин. {secs} сек." if mins > 0 else f"{secs} сек."

            text = (
                f"⏳ <b>Задача #{task.id} выполняется</b> (прошло {dur_str})\n\n"
                f"📁 Директория: <code>{html.escape(display_dir)}</code>\n"
                f"⚙️ Режим рассуждения: <code>{html.escape(task.effort.upper())}</code>\n"
                f"Статус: {status_desc}\n\n"
                "<i>Проверка активности: агент в процессе работы. "
                "Вы можете отменить задачу кнопкой ниже:</i>"
            )

            try:
                sent_msg = await self.bot.send_message(
                    chat_id=task.chat_id,
                    text=text,
                    reply_markup=build_task_cancel_keyboard(task.id),
                    parse_mode="HTML",
                )
                old_id = tracker[0]
                tracker[0] = getattr(sent_msg, "message_id", None) if sent_msg is not None else None

                if old_id is not None and hasattr(self.bot, "delete_message"):
                    try:
                        res = self.bot.delete_message(chat_id=task.chat_id, message_id=old_id)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as del_err:
                        logger.debug(
                            "Could not delete previous progress message %s: %s",
                            old_id,
                            del_err,
                        )
            except Exception as send_err:
                logger.warning(
                    "Failed sending progress update for Task #%d: %s", task.id, send_err
                )

    async def _process_single_task(self, task: TaskRecord) -> None:
        """Execute a single task with state transitions and Telegram notifications."""
        # Atomically transition QUEUED -> RUNNING in database
        marked = await self.task_store.mark_task_running(task.id)
        if not marked:
            logger.info("Task #%d was not in QUEUED state (possibly cancelled). Skipping.", task.id)
            return

        self._current_task_record = task
        display_dir = (
            str(task.workspace_path)
            if task.workspace_path
            else task.workspace_alias
        )

        logger.info("Starting execution of Task #%d (User: %d)...", task.id, task.user_id)

        # Tracking for progress notification message ID so each new minute check
        # replaces the previous one and is cleaned up on completion.
        tracker: list[Any | None] = [None]
        progress_task: asyncio.Task[None] | None = None

        if self.bot is not None:
            progress_task = asyncio.create_task(
                self._run_task_with_progress_notifications(task, display_dir, tracker)
            )
            self._pending_bg_tasks.add(progress_task)
            progress_task.add_done_callback(self._pending_bg_tasks.discard)

        # Execute via configured executor inside cancellable task
        async with self._cancel_lock:
            self._current_exec_task = asyncio.create_task(
                self.executor.execute_task(task)
            )

        try:
            result = await self._current_exec_task
        except asyncio.CancelledError:
            logger.info("Task #%d execution task was cancelled.", task.id)
            result = TaskResult(
                task_id=task.operation_id,
                status=TaskStatus.CANCELED,
                exit_code=130,
                error="Исполнение задачи было принудительно отменено.",
            )
        except Exception as e:
            logger.exception("CLI execution exception for Task #%d: %s", task.id, e)
            result = TaskResult(
                task_id=task.operation_id,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Внутренняя ошибка выполнения CLI: {e}",
            )
        finally:
            if progress_task and not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._current_exec_task = None
            self._current_task_record = None

        # Clean up the last progress message before delivering final result
        if self.bot is not None and tracker[0] is not None and hasattr(self.bot, "delete_message"):
            try:
                res = self.bot.delete_message(chat_id=task.chat_id, message_id=tracker[0])
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.debug("Could not delete last progress message %s: %s", tracker[0], e)

        # Save result file if there's response content
        result_file_path = None
        if result.response and result.response.strip():
            try:
                result_file_path = await self.task_store.save_result_file_async(
                    task.id, result.response
                )
            except Exception as e:
                logger.error("Failed saving result file for Task #%d: %s", task.id, e)

        # Record completion in SQLite
        await self.task_store.mark_task_finished(
            task_id=task.id,
            result=result,
            result_file_path=result_file_path,
        )
        logger.info(
            "Task #%d finished with status '%s' in %.2fs.",
            task.id,
            result.status.value,
            result.duration_seconds,
        )

        # Deliver result and files to Telegram user
        await self._deliver_result(task, result, display_dir, result_file_path)

    async def _deliver_result(
        self,
        task: TaskRecord,
        result: TaskResult,
        display_dir: str,
        result_file_path: Any,
    ) -> None:
        """Deliver execution result and documents to Telegram user."""
        if self.bot is None:
            return

        msg_text = _format_task_result_message(result, display_dir, task.id)
        keyboard = _build_post_execution_keyboard(task.operation_id)

        # 1. Deliver text message (with HTML and fallback)
        try:
            await self.bot.send_message(
                chat_id=task.chat_id,
                text=msg_text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except TelegramAPIError as e:
            logger.warning(
                "Failed delivering HTML result for Task #%d: %s. Trying plain text fallback.",
                task.id,
                e,
            )
            try:
                import re

                plain = re.sub(r"<[^>]+>", "", msg_text)
                await self.bot.send_message(
                    chat_id=task.chat_id,
                    text=plain[:4000],
                    reply_markup=keyboard,
                )
            except Exception as ex2:
                logger.error("Failed plain text delivery fallback for Task #%d: %s", task.id, ex2)
        except Exception as e:
            logger.debug("Non-Telegram error delivering result: %s", e)

        # 2. In a separate independent block, deliver full document if long or saved
        if result.response and len(result.response) > 3000:
            try:
                doc = BufferedInputFile(
                    result.response.encode("utf-8"),
                    filename=f"task_{task.id}_output.txt",
                )
                await self.bot.send_document(
                    chat_id=task.chat_id,
                    document=doc,
                    caption=f"📄 Полный вывод задачи #{task.id}",
                )
            except Exception as e:
                logger.warning("Failed delivering result document for Task #%d: %s", task.id, e)

    async def cancel_task(self, task_id: int, user_id: int) -> tuple[bool, str, TaskRecord | None]:
        """Cancel a task by ID whether it is queued or currently running."""
        # 1. Fetch current state from DB
        task = await self.task_store.get_task(task_id)
        if task is None:
            return False, f"Задача с ID <code>{task_id}</code> не найдена.", None

        if task.user_id != user_id:
            return False, f"Вы не являетесь владельцем задачи #{task_id}.", None

        # 2. Check if already finished
        if task.status not in (TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.RUNNING):
            status_desc = {
                TaskStatus.SUCCESS: "успешно выполнена",
                TaskStatus.PARTIAL_SUCCESS: "выполнена с ограничениями",
                TaskStatus.ERROR: "завершена с ошибкой",
                TaskStatus.CANCELED: "уже отменена",
                TaskStatus.INTERRUPTED: "прервана",
            }.get(task.status, task.status.value)
            return False, f"Задача #{task_id} {status_desc}. Её нельзя отменить.", task

        # 3. If currently RUNNING or ASSIGNED
        if task.status in (TaskStatus.RUNNING, TaskStatus.ASSIGNED):
            async with self._cancel_lock:
                if self._current_task_record and self._current_task_record.id == task_id:
                    if self._current_exec_task and not self._current_exec_task.done():
                        self._current_exec_task.cancel()
                await self.executor.cancel_task(task)
                # Wait briefly for cancellation to propagate and finalize
                await asyncio.sleep(0.5)

            # Re-fetch task state after cancellation attempt
            updated = await self.task_store.get_task(task_id)
            if updated and updated.status == TaskStatus.CANCELED:
                return (
                    True,
                    f"Выполняющаяся задача #{task_id} была успешно прервана и отменена.",
                    updated,
                )
            if updated and updated.status not in (TaskStatus.RUNNING, TaskStatus.ASSIGNED):
                # Race condition: finished just before cancellation
                return (
                    False,
                    f"Задача #{task_id} успела завершиться со статусом {updated.status.value}.",
                    updated,
                )
            # Force mark cancelled if CLI cancellation was acknowledged
            await self.task_store.mark_task_canceled(
                task_id, reason="Принудительно отменено пользователем."
            )
            updated = await self.task_store.get_task(task_id)
            return True, f"Выполняющаяся задача #{task_id} отменена.", updated

        # 4. If QUEUED
        success, err, updated_record = await self.task_store.cancel_queued_task(task_id, user_id)
        if success:
            return True, f"Задача #{task_id} отменена и удалена из очереди.", updated_record
        return False, err, updated_record
