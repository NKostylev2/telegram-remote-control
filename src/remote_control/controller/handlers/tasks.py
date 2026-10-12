"""Command handlers for /tasks, /cancel, and /result task lifecycle management."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from remote_control.common.models import TaskStatus
from remote_control.controller.keyboards import get_main_reply_keyboard
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import TaskRecord, TaskStore

tasks_router = Router(name="tasks_router")

_STATUS_EMOJIS = {
    TaskStatus.QUEUED: "⏳ В очереди",
    TaskStatus.ASSIGNED: "📥 Назначена агенту",
    TaskStatus.RUNNING: "🚀 Выполняется",
    TaskStatus.SUCCESS: "✅ Успешно",
    TaskStatus.PARTIAL_SUCCESS: "⚠️ С ограничениями",
    TaskStatus.ERROR: "❌ Ошибка",
    TaskStatus.CANCELED: "⚪ Отменена",
    TaskStatus.INTERRUPTED: "⏱️ Прервана",
}


def _format_task_summary_line(task: TaskRecord, queue_pos: int | None = None) -> str:
    """Format single task entry into a concise, readable block."""
    status_label = _STATUS_EMOJIS.get(task.status, task.status.value)
    if task.status == TaskStatus.QUEUED and queue_pos is not None:
        status_label = f"⏳ В очереди (позиция #{queue_pos})"

    prompt_clean = " ".join(task.prompt.split())
    prompt_snippet = prompt_clean[:60] + "..." if len(prompt_clean) > 60 else prompt_clean
    created_str = task.created_at.strftime("%d.%m %H:%M UTC")

    ws_name = (
        task.workspace_path.name
        if task.workspace_path
        else task.workspace_alias
    )

    return (
        f"<b>#{task.id}</b> [{status_label}]\n"
        f"💬 <i>{html.escape(prompt_snippet)}</i>\n"
        f"📁 <code>{html.escape(ws_name)}</code> | 🕒 {created_str}\n"
    )


@tasks_router.message(Command("tasks"))
@tasks_router.message(F.text.in_(["📋 Очередь задач", "Очередь задач", "📋 Задачи", "Задачи"]))
async def cmd_tasks(
    message: Message,
    task_store: TaskStore,
) -> None:
    """List recent tasks belonging to the requesting user."""
    user_id = message.from_user.id if message.from_user else 0
    tasks = await task_store.list_user_tasks(user_id=user_id, limit=10)

    if not tasks:
        await message.answer(
            "📋 <b>История задач пуста</b>\n\n"
            "У вас пока нет задач в очереди или истории.\n"
            "Используйте команду /run для запуска новой задачи.",
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard(),
        )
        return

    lines = ["📋 <b>Ваши последние задачи (до 10 шт.):</b>\n"]
    for task in tasks:
        q_pos = None
        if task.status == TaskStatus.QUEUED:
            q_pos = await task_store.get_queue_position(task.id)
        lines.append(_format_task_summary_line(task, q_pos))

    lines.append(
        "ℹ️ <i>Команды:</i>\n"
        "• <code>/cancel &lt;id&gt;</code> — отменить задачу\n"
        "• <code>/result &lt;id&gt;</code> — просмотреть результат"
    )
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard(),
    )


@tasks_router.message(Command("cancel"))
async def cmd_cancel(
    message: Message,
    command: CommandObject,
    task_queue: TaskQueue,
) -> None:
    """Cancel a pending or running task by integer ID."""
    raw_args = command.args.strip() if command.args else ""
    if not raw_args:
        await message.answer(
            "⚠️ <b>Укажите ID задачи для отмены:</b>\n"
            "Например: <code>/cancel 1</code>\n\n"
            "Используйте /tasks для просмотра списка ваших задач.",
            parse_mode="HTML",
        )
        return

    try:
        task_id = int(raw_args)
    except ValueError:
        await message.answer(
            f"❌ Неверный формат ID: <code>{html.escape(raw_args)}</code>. "
            "ID должен быть целым числом.",
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    success, msg, _task = await task_queue.cancel_task(task_id=task_id, user_id=user_id)

    if success:
        await message.answer(f"✅ {msg}", parse_mode="HTML")
    else:
        await message.answer(f"⚠️ {msg}", parse_mode="HTML")


@tasks_router.message(Command("result"))
async def cmd_result(
    message: Message,
    command: CommandObject,
    task_store: TaskStore,
) -> None:
    """View stored result and details of a finished task by ID."""
    raw_args = command.args.strip() if command.args else ""
    if not raw_args:
        await message.answer(
            "⚠️ <b>Укажите ID задачи для просмотра результата:</b>\n"
            "Например: <code>/result 1</code>\n\n"
            "Используйте /tasks для просмотра списка ваших задач.",
            parse_mode="HTML",
        )
        return

    try:
        task_id = int(raw_args)
    except ValueError:
        await message.answer(
            f"❌ Неверный формат ID: <code>{html.escape(raw_args)}</code>. "
            "ID должен быть целым числом.",
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    task = await task_store.get_task(task_id)

    if task is None or task.user_id != user_id:
        await message.answer(
            f"❌ Задача с ID <code>{task_id}</code> не найдена "
            "или принадлежит другому пользователю.",
            parse_mode="HTML",
        )
        return

    if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        status_label = _STATUS_EMOJIS.get(task.status, task.status.value)
        await message.answer(
            f"⏳ Задача #{task.id} ещё не завершена (текущий статус: <b>{status_label}</b>).\n"
            "Результат будет отправлен автоматически сразу после завершения выполнения.",
            parse_mode="HTML",
        )
        return

    # Task is completed / cancelled / interrupted / errored
    status_label = _STATUS_EMOJIS.get(task.status, task.status.value)
    dur = task.duration_seconds
    duration_str = f"{dur:.1f} сек." if dur is not None else "N/A"
    completed_str = (
        task.completed_at.strftime("%d.%m.%Y %H:%M UTC") if task.completed_at else "N/A"
    )
    ws_display = str(task.workspace_path) if task.workspace_path else task.workspace_alias

    lines = [
        f"📄 <b>Результат задачи #{task.id}</b> [{status_label}]\n",
        f"📁 Директория: <code>{html.escape(ws_display)}</code>",
        f"⏱️ Длительность: {duration_str}",
        f"🕒 Завершено: {completed_str}",
    ]

    if task.conversation_id:
        lines.append(f"🆔 Conversation ID: <code>{html.escape(task.conversation_id)}</code>")

    if task.soft_denied_tools:
        tools_str = ", ".join(task.soft_denied_tools)
        lines.append(f"⚠️ <i>Отклонённые инструменты:</i> <code>{html.escape(tools_str)}</code>")

    if task.error_message:
        lines.append(f"\n❌ <b>Ошибка:</b>\n<pre>{html.escape(task.error_message[:500])}</pre>")

    # Fetch output content
    output_content = None
    if task.result_file_path and task.result_file_path.is_file():
        try:
            output_content = task.result_file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if output_content is None and task.result_summary:
        output_content = task.result_summary

    if output_content and output_content.strip():
        if len(output_content) <= 3000:
            lines.append("\n📝 <b>Ответ модели:</b>")
            lines.append(f"<pre>{html.escape(output_content)}</pre>")
            await message.answer("\n".join(lines), parse_mode="HTML")
        else:
            lines.append("\n📝 <b>Ответ модели:</b> <i>(полный текст прикреплён файлом)</i>")
            await message.answer("\n".join(lines), parse_mode="HTML")
            doc = BufferedInputFile(
                output_content.encode("utf-8"),
                filename=f"task_{task.id}_output.txt",
            )
            await message.answer_document(
                doc,
                caption=f"📄 Результат выполнения задачи #{task.id}",
            )
    else:
        if not task.error_message:
            lines.append(
                "\n📝 <b>Результат:</b>\n"
                "<i>Действие выполнено на хосте (текстовый вывод отсутствует).</i>"
            )
        await message.answer("\n".join(lines), parse_mode="HTML")
