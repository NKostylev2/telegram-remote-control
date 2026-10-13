"""Command and callback handlers for /run and execution confirmation with interactive wizard."""

from __future__ import annotations

import asyncio
import html
import logging
import ntpath
import os
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from remote_control.agent.agy_client import AgyClient
from remote_control.agent.classifier import TaskProfile, classify_task
from remote_control.common.executor import TaskExecutor
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.browser import (
    get_available_drives,
    get_parent_directory,
    is_drive_root,
    list_subdirectories,
)
from remote_control.controller.confirmation import ConfirmationStatus, ConfirmationStore
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import (
    DuplicateOperationError,
    QueueFullError,
    TaskStore,
)

logger = logging.getLogger(__name__)

run_router = Router(name="run_router")

_MAX_TELEGRAM_MESSAGE_LENGTH = 4000
_PAGE_SIZE = 6

# User active workspaces cache (user_id -> Path)
_user_active_workspaces: dict[int, Path] = {}


def get_user_active_workspace(user_id: int) -> Path | None:
    """Return the currently selected working directory for the user."""
    return _user_active_workspaces.get(user_id)


def set_user_active_workspace(user_id: int, path: Path) -> None:
    """Update the active working directory for the user."""
    path_str = str(path)
    if ntpath.isabs(path_str) and os.name != "nt":
        _user_active_workspaces[user_id] = path
    else:
        try:
            _user_active_workspaces[user_id] = path.resolve()
        except Exception:
            _user_active_workspaces[user_id] = path


class RunWizard(StatesGroup):
    """FSM states for the interactive run configuration wizard."""

    choosing_directory = State()
    waiting_for_prompt = State()


# -----------------------------------------------------------------------------
# Keyboard builders
# -----------------------------------------------------------------------------


def _build_confirmation_keyboard(
    operation_id: str,
    effort: str = "low",
) -> InlineKeyboardMarkup:
    """Build inline keyboard for human confirmation with reasoning effort toggle."""
    effort_labels = {
        "low": "⚡ Режим: Быстрый (low)",
        "medium": "⚖️ Режим: Средний (medium)",
        "high": "🧠 Режим: Глубокий (high)",
    }
    btn_text = effort_labels.get(effort, f"⚡ Режим: {effort}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"ef:{operation_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Да, запустить",
                    callback_data=f"cf:{operation_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Нет, отменить",
                    callback_data=f"cl:{operation_id}",
                ),
            ]
        ]
    )


def _format_confirmation_text(
    interpretation: str | None = None,
    target_path: Path | None = None,
    raw_prompt: str = "",
    effort: str = "low",
    workspace_alias: str | None = None,
    profile: TaskProfile | None = None,
) -> str:
    """Format confirmation message with prompt, directory, category, and reasoning effort."""
    effort_labels = {
        "low": "⚡ Быстрый (low) — простое действие / сбор данных",
        "medium": "⚖️ Средний (medium) — умеренная сложность",
        "high": "🧠 Глубокий (high) — сложный анализ и кодинг",
    }
    effort_desc = effort_labels.get(effort, effort)
    path_display = str(target_path) if target_path is not None else "не указана"
    if workspace_alias and workspace_alias != path_display:
        dir_display = f"{workspace_alias} ({path_display})"
    else:
        dir_display = path_display

    category_info = ""
    if profile is not None:
        category_info = (
            f"🏷️ <b>Категория:</b> <code>{html.escape(profile.label)}</code>\n"
            f"<i>{html.escape(profile.description)}</i>\n\n"
        )

    interp_info = ""
    if interpretation and interpretation.strip():
        interp_info = (
            "💡 <b>Понимание задачи агентом:</b>\n"
            f"<blockquote>{html.escape(interpretation)}</blockquote>\n\n"
        )

    return (
        "🤖 <b>Правильно ли агент понял задачу?</b>\n\n"
        f"💬 <b>Исходный запрос:</b>\n<blockquote>{html.escape(raw_prompt)}</blockquote>\n\n"
        f"{category_info}"
        f"{interp_info}"
        f"📁 <b>Рабочая директория:</b>\n<code>{html.escape(dir_display)}</code>\n\n"
        f"⚙️ <b>Режим рассуждения:</b> <code>{html.escape(effort_desc)}</code>\n\n"
        "<i>Подтвердите запуск или нажмите кнопку режима для переключения:</i>"
    )


def _build_workspace_selection_keyboard(
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for choosing preset workspace or switching to drive picker."""
    buttons: list[list[InlineKeyboardButton]] = []

    workspaces = {}
    if (
        config is not None
        and getattr(config, "controller_mode", "local") == "distributed"
        and agent_dispatcher is not None
    ):
        workspaces = agent_dispatcher.get_available_workspaces()
    elif config is not None:
        workspaces = {k: str(v) for k, v in config.allowed_workspaces.items()}

    # Preset workspaces
    for alias, path_val in workspaces.items():
        state_icon = "🏠" if alias.lower() in ("control", "home", "default") else "📁"
        base_name = ntpath.basename(str(path_val).rstrip("\\/")) or str(path_val)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{state_icon} {alias} ({base_name})",
                    callback_data=f"wz:ws:{alias}",
                )
            ]
        )

    # Disk navigation button
    buttons.append(
        [
            InlineKeyboardButton(
                text="💾 Выбрать диск...",
                callback_data="wz:drives",
            )
        ]
    )

    # Cancel button
    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="wz:cancel",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _build_drives_keyboard(
    drives: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard listing available system drives asynchronously."""
    if drives is None:
        drives = await asyncio.to_thread(get_available_drives)
    drive_buttons: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []

    for d in drives:
        letter = d.rstrip(":\\").rstrip("/")
        drive_buttons.append(
            InlineKeyboardButton(
                text=f"💽 Диск {d}",
                callback_data=f"wz:drv:{letter}",
            )
        )

    # 2 buttons per row
    for i in range(0, len(drive_buttons), 2):
        rows.append(drive_buttons[i : i + 2])

    rows.append(
        [
            InlineKeyboardButton(text="⬅️ К воркспейсам", callback_data="wz:home"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="wz:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_folder_keyboard(
    subdirs: list[Path], page: int, current_path: Path
) -> InlineKeyboardMarkup:
    """Build folder navigation keyboard with pagination and actions."""
    total_items = len(subdirs)
    total_pages = max(1, (total_items + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * _PAGE_SIZE
    end_idx = min(start_idx + _PAGE_SIZE, total_items)
    page_items = subdirs[start_idx:end_idx]

    rows: list[list[InlineKeyboardButton]] = []

    # Subdirectory buttons (2 per row)
    row: list[InlineKeyboardButton] = []
    for idx_offset, item in enumerate(page_items):
        actual_idx = start_idx + idx_offset
        folder_name = ntpath.basename(str(item).rstrip("\\/")) or str(item)
        folder_btn = InlineKeyboardButton(
            text=f"📁 {folder_name[:24]}",
            callback_data=f"wz:dir:{actual_idx}",
        )
        row.append(folder_btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Pagination controls if more than one page
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="◀️ Пред.", callback_data=f"wz:page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"📄 {page + 1}/{total_pages}",
                callback_data="wz:noop",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(text="След. ▶️", callback_data=f"wz:page:{page + 1}")
            )
        rows.append(nav_row)

    # Current folder selection button
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Остаться в этой папке",
                callback_data="wz:select",
            )
        ]
    )

    # Navigation up and cancel buttons
    up_button = (
        InlineKeyboardButton(text="⬆️ На уровень вверх", callback_data="wz:up")
        if not is_drive_root(current_path)
        else InlineKeyboardButton(text="⬅️ К выбору диска", callback_data="wz:drives")
    )

    rows.append(
        [
            up_button,
            InlineKeyboardButton(text="❌ Отмена", callback_data="wz:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_prompt_cancel_keyboard() -> InlineKeyboardMarkup:
    """Build cancel button for prompt input stage."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="wz:cancel",
                )
            ]
        ]
    )


def _build_post_execution_keyboard(operation_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard offering another prompt in the same directory or changing directory."""
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


def _format_task_result_message(result: TaskResult, workspace_alias: str) -> str:
    """Format TaskResult safely into an informative text message."""
    status_header = {
        TaskStatus.SUCCESS: "✅ <b>Задача выполнена</b>",
        TaskStatus.PARTIAL_SUCCESS: "⚠️ <b>Задача выполнена с ограничениями</b>",
        TaskStatus.ERROR: "❌ <b>Ошибка при выполнении задачи</b>",
        TaskStatus.CANCELED: "⚪ <b>Задача отменена</b>",
        TaskStatus.INTERRUPTED: "⏱️ <b>Выполнение прервано по таймауту</b>",
    }.get(result.status, f"ℹ️ <b>Статус: {result.status}</b>")

    lines = [
        status_header,
        f"📁 Директория: <code>{html.escape(workspace_alias)}</code>",
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
        if len(resp_text) > 2500:
            snippet = resp_text[:2500]
            lines.append(f"<pre>{html.escape(snippet)}</pre>")
            lines.append("<i>[... полный вывод обрезан по лимиту Telegram]</i>")
        else:
            lines.append(f"<pre>{html.escape(resp_text)}</pre>")
    elif not result.error:
        lines.append(
            "\n📝 <b>Результат:</b> "
            "<i>Действие выполнено на хосте (текстовый вывод отсутствует).</i>"
        )

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# /run command and interactive wizard
# -----------------------------------------------------------------------------


@run_router.message(Command("run"))
@run_router.message(F.text.in_(["🚀 Запустить задачу", "Запустить задачу"]))
async def cmd_run(
    message: Message,
    command: CommandObject | None = None,
    config: AppConfig | None = None,
    agy_client: AgyClient | None = None,
    confirmation_store: ConfirmationStore | None = None,
    task_lock: asyncio.Lock | None = None,
    state: FSMContext | None = None,
    task_store: TaskStore | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
    executor: TaskExecutor | None = None,
) -> None:
    """Handle /run command or button click: start interactive wizard or use direct arguments."""
    # 1. If arguments are provided, use direct fast-track execution flow
    if command is not None and command.args:
        parts = command.args.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer(
                "⚠️ Укажите текст задачи после псевдонима рабочей директории.\n"
                "Пример: <code>/run control Проверь статус репозитория</code>\n\n"
                "Либо введите просто <code>/run</code> для интерактивного выбора.",
                parse_mode="HTML",
            )
            return

        workspace_alias = parts[0].strip()
        prompt = parts[1].strip()

        if not prompt:
            await message.answer("❌ Текст задачи не может быть пустым.")
            return

        if "\x00" in prompt:
            await message.answer("❌ Текст задачи содержит недопустимые символы.")
            return

        if len(prompt) > 2000:
            await message.answer("❌ Текст задачи превышает лимит (максимум 2000 символов).")
            return

        if workspace_alias not in config.allowed_workspaces:
            await message.answer(
                f"❌ Рабочее пространство <code>{html.escape(workspace_alias)}</code> "
                "не найдено в списке сохранённых.\n"
                "Используйте /workspace для просмотра или вызовите /run для выбора диска.",
                parse_mode="HTML",
            )
            return

        target_path = config.allowed_workspaces[workspace_alias]
        mode = getattr(config, "controller_mode", "local")
        if mode != "distributed":
            if not target_path.exists() or not target_path.is_dir():
                await message.answer(
                    f"❌ Каталог <code>{html.escape(str(target_path))}</code> не найден на диске.",
                    parse_mode="HTML",
                )
                return

            if agy_client is not None and not agy_client.is_cli_available():
                await message.answer(
                    "❌ Antigravity CLI не найден на компьютере. Запуск невозможен."
                )
                return
        else:
            if agent_dispatcher is not None and agent_dispatcher.is_agent_connected():
                if not agent_dispatcher.is_agent_ready():
                    await message.answer(
                        "❌ Antigravity CLI не найден на удалённом компьютере Windows. "
                        "Запуск невозможен."
                    )
                    return

        if task_store is None and task_lock is not None and task_lock.locked():
            await message.answer("⚠️ Хост занят выполнением другой задачи. Подождите её завершения.")
            return

        t0 = time.monotonic()
        profile = classify_task(prompt)
        t_classify = time.monotonic() - t0
        logger.info(
            "Stage [classify_task] in cmd_run took %.3f ms (category=%s, effort=%s)",
            t_classify * 1000,
            profile.task_type,
            profile.effort,
        )

        user_id = message.from_user.id if message.from_user else 0
        chat_id = message.chat.id
        op = confirmation_store.create_operation(
            user_id=user_id,
            chat_id=chat_id,
            workspace_alias=workspace_alias,
            prompt=prompt,
            workspace_path=target_path,
            interpretation=profile.description,
            effort=profile.effort,
        )

        confirm_text = _format_confirmation_text(
            interpretation=profile.description,
            target_path=target_path,
            raw_prompt=prompt,
            effort=op.effort,
            workspace_alias=workspace_alias,
            profile=profile,
        )
        keyboard = _build_confirmation_keyboard(op.operation_id, effort=op.effort)
        t_send = time.monotonic()
        await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")
        logger.info(
            "Stage [show_confirmation] in cmd_run took %.3f ms (op_id=%s)",
            (time.monotonic() - t_send) * 1000,
            op.operation_id,
        )
        return

    # 2. No arguments provided: launch interactive wizard
    await state.set_state(RunWizard.choosing_directory)
    keyboard = _build_workspace_selection_keyboard(config, agent_dispatcher=agent_dispatcher)

    text = (
        "📁 <b>Выберите рабочую директорию для запуска:</b>\n\n"
        "Вы можете выбрать сохранённое рабочее пространство либо перейти "
        "к выбору диска и папок на компьютере."
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


# -----------------------------------------------------------------------------
# Wizard callback handlers
# -----------------------------------------------------------------------------


@run_router.callback_query(F.data == "wz:home")
async def cb_wizard_home(
    callback: CallbackQuery,
    config: AppConfig,
    state: FSMContext,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Return to the initial workspace selection screen."""
    await state.set_state(RunWizard.choosing_directory)
    keyboard = _build_workspace_selection_keyboard(config, agent_dispatcher=agent_dispatcher)
    text = (
        "📁 <b>Выберите рабочую директорию для запуска:</b>\n\n"
        "Вы можете выбрать сохранённое рабочее пространство либо перейти "
        "к выбору диска и папок на компьютере."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data.startswith("wz:ws:"))
async def cb_wizard_select_workspace(
    callback: CallbackQuery,
    config: AppConfig,
    state: FSMContext,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Select a pre-configured workspace alias."""
    alias = callback.data.split(":", 2)[2]
    target_path = None
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"

    if is_distributed and agent_dispatcher is not None:
        agent_workspaces = agent_dispatcher.get_available_workspaces()
        if alias in agent_workspaces:
            target_path = Path(agent_workspaces[alias])

    if target_path is None:
        if alias not in config.allowed_workspaces:
            await callback.answer("Рабочее пространство не найдено.", show_alert=True)
            return
        target_path = config.allowed_workspaces[alias]

    if not is_distributed:
        if not target_path.exists() or not target_path.is_dir():
            await callback.answer(
                f"Каталог не найден на диске: {target_path}",
                show_alert=True,
            )
            return

    user_id = callback.from_user.id if callback.from_user else 0
    set_user_active_workspace(user_id, target_path)

    await state.set_state(RunWizard.waiting_for_prompt)
    await state.update_data(
        selected_path=str(target_path),
        workspace_alias=alias,
    )

    text = (
        f"📁 <b>Выбрана рабочая директория:</b>\n<code>{html.escape(str(target_path))}</code> "
        f"(<b>{html.escape(alias)}</b>)\n\n"
        "💬 <b>Отправьте в чат текст команды/задачи для агента:</b>\n"
        "<i>(например: «Проверь git status и напиши краткий отчёт»)</i>"
    )
    keyboard = _build_prompt_cancel_keyboard()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data == "wz:drives")
async def cb_wizard_show_drives(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Display available system drives."""
    is_distributed = (
        config is not None and getattr(config, "controller_mode", "local") == "distributed"
    )
    if is_distributed:
        if agent_dispatcher is not None and not agent_dispatcher.is_agent_connected():
            await callback.answer(
                "⚠️ Windows Agent оффлайн. Включите ПК (/wake).",
                show_alert=True,
            )
            return
        drives = agent_dispatcher.get_available_drives() if agent_dispatcher else ["C:\\"]
    else:
        drives = await asyncio.to_thread(get_available_drives)

    await state.set_state(RunWizard.choosing_directory)
    keyboard = await _build_drives_keyboard(drives)
    text = "💾 <b>Выберите системный диск:</b>"
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data.startswith("wz:drv:"))
async def cb_wizard_select_drive(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Open root folder of the selected drive."""
    letter = callback.data.split(":", 2)[2]
    drive_str = f"{letter}:\\"
    drive_path = Path(drive_str)
    is_distributed = (
        config is not None and getattr(config, "controller_mode", "local") == "distributed"
    )

    if is_distributed and agent_dispatcher is not None:
        if not agent_dispatcher.is_agent_connected():
            await callback.answer("⚠️ Windows Agent оффлайн.", show_alert=True)
            return
        subdirs = await agent_dispatcher.request_remote_subdirectories(drive_str)
    else:
        if not drive_path.exists():
            await callback.answer(f"Диск {letter}:\\ недоступен.", show_alert=True)
            return
        subdirs = await asyncio.to_thread(list_subdirectories, drive_path)

    await state.update_data(
        current_path=drive_str,
        subdirs=[str(p) for p in subdirs],
        page=0,
    )

    keyboard = _build_folder_keyboard(subdirs, 0, drive_path)
    folders_status = f"Найдено папок: {len(subdirs)}" if subdirs else "Вложенных папок не найдено"
    text = (
        f"📂 <b>Текущий путь:</b> <code>{html.escape(drive_str)}</code>\n"
        f"<i>{folders_status}</i>\n\n"
        "Выберите подпапку или нажмите <b>«Остаться в этой папке»</b>:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data.startswith("wz:dir:"))
async def cb_wizard_open_folder(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Navigate into a selected subdirectory."""
    idx = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    subdirs_raw = data.get("subdirs", [])

    if idx < 0 or idx >= len(subdirs_raw):
        await callback.answer("Папка не найдена в списке.", show_alert=True)
        return

    target_path_str = subdirs_raw[idx]
    target_path = Path(target_path_str)
    is_distributed = (
        config is not None and getattr(config, "controller_mode", "local") == "distributed"
    )

    if is_distributed and agent_dispatcher is not None:
        if not agent_dispatcher.is_agent_connected():
            await callback.answer("⚠️ Windows Agent оффлайн.", show_alert=True)
            return
        new_subdirs = await agent_dispatcher.request_remote_subdirectories(target_path_str)
    else:
        if not target_path.exists() or not target_path.is_dir():
            await callback.answer(
                "⚠️ Нет доступа к этой папке или она была удалена.", show_alert=True
            )
            return
        new_subdirs = await asyncio.to_thread(list_subdirectories, target_path)

    await state.update_data(
        current_path=target_path_str,
        subdirs=[str(p) for p in new_subdirs],
        page=0,
    )

    keyboard = _build_folder_keyboard(new_subdirs, 0, target_path)
    folders_status = (
        f"Найдено папок: {len(new_subdirs)}" if new_subdirs else "Вложенных папок не найдено"
    )
    text = (
        f"📂 <b>Текущий путь:</b> <code>{html.escape(target_path_str)}</code>\n"
        f"<i>{folders_status}</i>\n\n"
        "Выберите подпапку или нажмите <b>«Остаться в этой папке»</b>:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data.startswith("wz:page:"))
async def cb_wizard_change_page(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Paginate subdirectories list."""
    page = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    current_path_str = data.get("current_path")
    subdirs_raw = data.get("subdirs", [])

    if not current_path_str:
        await callback.answer()
        return

    current_path = Path(current_path_str)
    subdirs = [Path(p) for p in subdirs_raw]
    await state.update_data(page=page)

    keyboard = _build_folder_keyboard(subdirs, page, current_path)
    text = (
        f"📂 <b>Текущий путь:</b> <code>{html.escape(str(current_path))}</code>\n"
        f"<i>Найдено папок: {len(subdirs)}</i>\n\n"
        "Выберите подпапку или нажмите <b>«Остаться в этой папке»</b>:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data == "wz:up")
async def cb_wizard_navigate_up(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Navigate up to parent directory or drives list."""
    data = await state.get_data()
    current_path_str = data.get("current_path")
    if not current_path_str:
        await cb_wizard_show_drives(
            callback, state, config=config, agent_dispatcher=agent_dispatcher
        )
        return

    current_path = Path(current_path_str)
    parent = get_parent_directory(current_path)

    if parent is None or is_drive_root(current_path):
        # Back to drive selection
        await cb_wizard_show_drives(
            callback, state, config=config, agent_dispatcher=agent_dispatcher
        )
        return

    parent_str = str(parent)
    is_distributed = (
        config is not None and getattr(config, "controller_mode", "local") == "distributed"
    )
    if is_distributed and agent_dispatcher is not None:
        if not agent_dispatcher.is_agent_connected():
            await callback.answer("⚠️ Windows Agent оффлайн.", show_alert=True)
            return
        subdirs = await agent_dispatcher.request_remote_subdirectories(parent_str)
    else:
        subdirs = await asyncio.to_thread(list_subdirectories, parent)

    await state.update_data(
        current_path=parent_str,
        subdirs=[str(p) for p in subdirs],
        page=0,
    )

    keyboard = _build_folder_keyboard(subdirs, 0, parent)
    folders_status = f"Найдено папок: {len(subdirs)}" if subdirs else "Вложенных папок не найдено"
    text = (
        f"📂 <b>Текущий путь:</b> <code>{html.escape(parent_str)}</code>\n"
        f"<i>{folders_status}</i>\n\n"
        "Выберите подпапку или нажмите <b>«Остаться в этой папке»</b>:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data == "wz:select")
async def cb_wizard_select_current_folder(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Confirm current folder and advance to prompt input step."""
    data = await state.get_data()
    current_path_str = data.get("current_path")
    if not current_path_str:
        await callback.answer("Директория не выбрана.", show_alert=True)
        return

    selected_path = Path(current_path_str)
    alias = ntpath.basename(current_path_str.rstrip("\\/")) or current_path_str

    user_id = callback.from_user.id if callback.from_user else 0
    set_user_active_workspace(user_id, selected_path)

    await state.set_state(RunWizard.waiting_for_prompt)
    await state.update_data(
        selected_path=current_path_str,
        workspace_alias=alias,
    )

    text = (
        f"📁 <b>Выбрана рабочая директория:</b>\n<code>{html.escape(current_path_str)}</code>\n\n"
        "💬 <b>Отправьте в чат текст команды/задачи для агента:</b>\n"
        "<i>(например: «Проверь git status и напиши краткий отчёт»)</i>"
    )
    keyboard = _build_prompt_cancel_keyboard()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data == "wz:noop")
async def cb_wizard_noop(callback: CallbackQuery) -> None:
    """No-operation handler for decorative button labels."""
    await callback.answer()


@run_router.callback_query(F.data == "wz:cancel")
async def cb_wizard_cancel(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Cancel the wizard and clear state."""
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("❌ Выбор директории отменён.", reply_markup=None)
    await callback.answer("Отменено.")


# -----------------------------------------------------------------------------
# Prompt input handler
# -----------------------------------------------------------------------------


@run_router.message(RunWizard.waiting_for_prompt)
async def message_received_prompt(
    message: Message,
    state: FSMContext,
    confirmation_store: ConfirmationStore,
    agy_client: AgyClient,
    task_lock: asyncio.Lock,
    task_store: TaskStore | None = None,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
    executor: TaskExecutor | None = None,
) -> None:
    """Receive task prompt text from user and build final confirmation message."""
    raw_text = (message.text or "").strip()

    if raw_text.startswith("/"):
        if raw_text.lower() == "/cancel":
            await state.clear()
            await message.answer("❌ Ввод задачи отменён.")
            return

    if not raw_text:
        await message.answer("❌ Текст задачи не может быть пустым. Отправьте описание задачи:")
        return

    if "\x00" in raw_text:
        await message.answer("❌ Текст задачи содержит недопустимые символы. Отправьте снова:")
        return

    if len(raw_text) > 2000:
        await message.answer(
            "❌ Текст задачи превышает лимит (максимум 2000 символов). "
            "Пожалуйста, сократите описание:"
        )
        return

    if config is not None and getattr(config, "controller_mode", "local") == "distributed":
        if agent_dispatcher is not None and agent_dispatcher.is_agent_connected():
            if not agent_dispatcher.is_agent_ready():
                await state.clear()
                await message.answer(
                    "❌ Antigravity CLI не найден на удалённом компьютере Windows. "
                    "Запуск невозможен."
                )
                return
    else:
        if agy_client is not None and not agy_client.is_cli_available():
            await state.clear()
            await message.answer(
                "❌ Antigravity CLI не найден на компьютере. Запуск невозможен."
            )
            return

    if task_store is None and task_lock is not None and task_lock.locked():
        await message.answer("⚠️ Хост занят выполнением другой задачи. Пожалуйста, подождите.")
        return

    data = await state.get_data()
    selected_path_str = data.get("selected_path")
    workspace_alias = data.get("workspace_alias") or (
        ntpath.basename(selected_path_str.rstrip("\\/")) if selected_path_str else "workspace"
    )
    await state.clear()

    if not selected_path_str:
        await message.answer(
            "❌ Ошибка: путь директории не был зафиксирован. Запустите /run снова."
        )
        return

    target_path = Path(selected_path_str)
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    set_user_active_workspace(user_id, target_path)

    t0 = time.monotonic()
    profile = classify_task(raw_text)
    t_classify = time.monotonic() - t0
    logger.info(
        "Stage [classify_task] in wizard took %.3f ms (category=%s, effort=%s)",
        t_classify * 1000,
        profile.task_type,
        profile.effort,
    )

    op = confirmation_store.create_operation(
        user_id=user_id,
        chat_id=chat_id,
        workspace_alias=workspace_alias,
        prompt=raw_text,
        workspace_path=target_path,
        interpretation=profile.description,
        effort=profile.effort,
    )

    confirm_text = _format_confirmation_text(
        interpretation=profile.description,
        target_path=target_path,
        raw_prompt=raw_text,
        effort=op.effort,
        workspace_alias=workspace_alias,
        profile=profile,
    )
    keyboard = _build_confirmation_keyboard(op.operation_id, effort=op.effort)
    t_send = time.monotonic()
    await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")
    logger.info(
        "Stage [show_confirmation] in wizard took %.3f ms (op_id=%s)",
        (time.monotonic() - t_send) * 1000,
        op.operation_id,
    )


@run_router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def message_direct_prompt(
    message: Message,
    config: AppConfig,
    confirmation_store: ConfirmationStore,
    agy_client: AgyClient,
    task_lock: asyncio.Lock,
    task_store: TaskStore | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
    executor: TaskExecutor | None = None,
) -> None:
    """Handle direct text prompt messages from chat outside FSM wizard."""
    raw_text = (message.text or "").strip()

    # Skip known bottom panel button labels
    known_buttons = {
        "🚀 Запустить задачу",
        "Запустить задачу",
        "📸 Снимок экрана",
        "Снимок экрана",
        "📊 Статус",
        "Статус",
        "📋 Очередь задач",
        "Очередь задач",
        "📋 Задачи",
        "Задачи",
        "📁 Рабочая папка",
        "Рабочая папка",
        "📁 Каталоги",
        "Каталоги",
        "❓ Помощь",
        "Помощь",
        "Справка",
    }
    if raw_text in known_buttons:
        return

    if not raw_text:
        return

    if "\x00" in raw_text:
        await message.answer("❌ Текст задачи содержит недопустимые символы.")
        return

    if len(raw_text) > 2000:
        await message.answer("❌ Текст задачи превышает лимит (максимум 2000 символов).")
        return

    if getattr(config, "controller_mode", "local") == "distributed":
        if agent_dispatcher is not None and agent_dispatcher.is_agent_connected():
            if not agent_dispatcher.is_agent_ready():
                await message.answer(
                    "❌ Antigravity CLI не найден на удалённом компьютере Windows. "
                    "Запуск невозможен."
                )
                return
    else:
        if agy_client is not None and not agy_client.is_cli_available():
            await message.answer(
                "❌ Antigravity CLI не найден на компьютере. Запуск невозможен."
            )
            return

    if task_store is None and task_lock.locked():
        await message.answer("⚠️ Хост занят выполнением другой задачи. Пожалуйста, подождите.")
        return

    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id

    target_path = get_user_active_workspace(user_id)
    workspace_alias = None

    if target_path is None:
        if config.allowed_workspaces:
            first_alias, first_path = next(iter(config.allowed_workspaces.items()))
            target_path = first_path
            workspace_alias = first_alias
            set_user_active_workspace(user_id, target_path)

    if target_path is None or (
        getattr(config, "controller_mode", "local") != "distributed" and not target_path.exists()
    ):
        await message.answer(
            "📁 <b>Рабочая директория не выбрана.</b>\n\n"
            "Пожалуйста, выберите папку через команду /run перед отправкой задач.",
            parse_mode="HTML",
        )
        return

    if workspace_alias is None:
        for alias, p in config.allowed_workspaces.items():
            if str(p).rstrip("\\/").lower() == str(target_path).rstrip("\\/").lower():
                workspace_alias = alias
                break
        if workspace_alias is None:
            workspace_alias = ntpath.basename(str(target_path).rstrip("\\/")) or "workspace"

    t0 = time.monotonic()
    profile = classify_task(raw_text)
    t_classify = time.monotonic() - t0
    logger.info(
        "Stage [classify_task] in direct_prompt took %.3f ms (category=%s, effort=%s)",
        t_classify * 1000,
        profile.task_type,
        profile.effort,
    )

    op = confirmation_store.create_operation(
        user_id=user_id,
        chat_id=chat_id,
        workspace_alias=workspace_alias,
        prompt=raw_text,
        workspace_path=target_path,
        interpretation=profile.description,
        effort=profile.effort,
    )

    confirm_text = _format_confirmation_text(
        interpretation=profile.description,
        target_path=target_path,
        raw_prompt=raw_text,
        effort=op.effort,
        workspace_alias=workspace_alias,
        profile=profile,
    )
    keyboard = _build_confirmation_keyboard(op.operation_id, effort=op.effort)
    t_send = time.monotonic()
    await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")
    logger.info(
        "Stage [show_confirmation] in direct_prompt took %.3f ms (op_id=%s)",
        (time.monotonic() - t_send) * 1000,
        op.operation_id,
    )


# -----------------------------------------------------------------------------
# Confirmation callbacks (Yes / No)
# -----------------------------------------------------------------------------


@run_router.callback_query(F.data.startswith("cl:"))
async def callback_cancel_run(
    callback: CallbackQuery,
    confirmation_store: ConfirmationStore,
) -> None:
    """Handle cancellation of a pending /run confirmation."""
    if not callback.data:
        return

    op_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else 0
    chat_id = callback.message.chat.id if callback.message and callback.message.chat else 0

    success, err_msg, _op = confirmation_store.cancel_operation(op_id, user_id, chat_id)
    if not success:
        await callback.answer(err_msg, show_alert=True)
        return

    await callback.answer("Запуск отменён.")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "❌ Запуск задачи отменён пользователем.",
            reply_markup=None,
        )


@run_router.callback_query(F.data.startswith("ef:"))
async def callback_toggle_effort(
    callback: CallbackQuery,
    confirmation_store: ConfirmationStore,
) -> None:
    """Toggle reasoning effort level on the confirmation screen."""
    if not callback.data:
        return

    op_id = callback.data.split(":", 1)[1]
    op = confirmation_store.get_operation(op_id)
    if op is None or op.status != ConfirmationStatus.PENDING:
        await callback.answer("Операция устарела или уже запущена.", show_alert=True)
        return

    # Cycle: low -> medium -> high -> low
    cycle = {"low": "medium", "medium": "high", "high": "low"}
    next_effort = cycle.get(op.effort, "low")
    confirmation_store.set_effort(op_id, next_effort)

    target_path = op.workspace_path or Path(".")
    text = _format_confirmation_text(
        interpretation=op.interpretation or "",
        target_path=target_path,
        raw_prompt=op.prompt,
        effort=next_effort,
        workspace_alias=op.workspace_alias,
    )
    keyboard = _build_confirmation_keyboard(op.operation_id, effort=next_effort)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer(f"Режим: {next_effort.upper()}")


@run_router.callback_query(F.data.startswith("cf:"))
async def callback_confirm_run(
    callback: CallbackQuery,
    confirmation_store: ConfirmationStore,
    agy_client: AgyClient,
    task_lock: asyncio.Lock,
    task_store: TaskStore | None = None,
    task_queue: TaskQueue | None = None,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Handle approval of a pending /run confirmation and launch or queue CLI task."""
    if not callback.data:
        return

    op_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id if callback.from_user else 0
    chat_id = callback.message.chat.id if callback.message and callback.message.chat else 0

    # 1. Fallback concurrency check if persistent queue is not enabled
    if task_store is None and task_lock.locked():
        await callback.answer(
            "Хост в данный момент занят выполнением другой задачи. Повторите попытку позже.",
            show_alert=True,
        )
        return

    # 2. Claim operation
    success, err_msg, op = confirmation_store.confirm_operation(op_id, user_id, chat_id)
    if not success or op is None:
        await callback.answer(err_msg, show_alert=True)
        return

    display_target = str(op.workspace_path) if op.workspace_path else op.workspace_alias

    # 3. If persistent task store is present, persist into SQLite queue
    if task_store is not None:
        try:
            t_enq = time.monotonic()
            task_record = await task_store.enqueue_task(op)
            logger.info(
                "Stage [enqueue_task] took %.3f ms (task_id=%d, op_id=%s)",
                (time.monotonic() - t_enq) * 1000,
                task_record.id,
                op_id,
            )
        except QueueFullError as e:
            logger.warning("Queue limit reached for op %s: %s", op_id, e)
            await callback.answer(
                "⚠️ Очередь задач переполнена. Подождите завершения текущих задач.",
                show_alert=True,
            )
            return
        except DuplicateOperationError:
            await callback.answer("Эта задача уже находится в очереди.", show_alert=True)
            return
        except Exception as e:
            logger.exception("Failed to enqueue task into SQLite: %s", e)
            await callback.answer("Ошибка сохранения задачи в базу данных.", show_alert=True)
            return

        pos = await task_store.get_queue_position(task_record.id)
        if task_queue is not None:
            task_queue.notify()

        confirmation_store.mark_completed(op_id)
        await callback.answer("Запуск подтверждён!")

        if isinstance(callback.message, Message):
            ws_escaped = html.escape(display_target)
            pos_str = (
                f"Ваша позиция в очереди: <b>{pos}</b>."
                if pos > 1
                else "Задача будет запущена первой."
            )
            offline_note = ""
            if (
                config is not None
                and getattr(config, "controller_mode", "local") == "distributed"
                and agent_dispatcher is not None
                and not agent_dispatcher.is_agent_connected()
            ):
                offline_note = (
                    "\n\n⚠️ <i>Windows Agent сейчас оффлайн. "
                    "Задача выполнится автоматически после включения ПК (/wake).</i>"
                )
            await callback.message.edit_text(
                f"📥 <b>Задача #{task_record.id} добавлена в очередь!</b>\n\n"
                f"📁 Рабочая директория: <code>{ws_escaped}</code>\n"
                f"⚙️ Режим рассуждения: <code>{html.escape(op.effort.upper())}</code>\n"
                f"📊 {pos_str}"
                f"{offline_note}\n\n"
                "<i>Бот уведомит вас о начале выполнения и пришлёт итоговый результат.</i>",
                reply_markup=None,
                parse_mode="HTML",
            )
        return

    # Fallback legacy execution path (without persistent queue)
    await callback.answer("Запуск подтверждён!")

    display_target = str(op.workspace_path) if op.workspace_path else op.workspace_alias
    if isinstance(callback.message, Message):
        ws_escaped = html.escape(display_target)
        await callback.message.edit_text(
            f"🚀 <b>Задача выполняется в директории:</b>\n<code>{ws_escaped}</code>\n\n"
            "Выполняется вызов Antigravity CLI, пожалуйста, ожидайте ответ...",
            reply_markup=None,
            parse_mode="HTML",
        )

    # 3. Execute with concurrency lock
    async with task_lock:
        try:
            result = await agy_client.execute(
                prompt=op.prompt,
                workspace_alias=op.workspace_alias if not op.workspace_path else None,
                workspace_path=op.workspace_path,
                effort=op.effort,
                dangerously_skip_permissions=getattr(config, "cli_auto_approve_permissions", True),
            )
        except Exception as e:
            logger.exception("Unexpected error during task execution: %s", e)
            result = TaskResult(
                task_id=op.operation_id,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Внутренняя ошибка запуска: {e}",
            )
        finally:
            confirmation_store.mark_completed(op_id)

    # 4. Deliver result
    msg_text = _format_task_result_message(result, display_target)
    post_keyboard = _build_post_execution_keyboard(op_id)
    if callback.message:
        await callback.message.answer(
            msg_text,
            reply_markup=post_keyboard,
            parse_mode="HTML",
        )
        if result.response and len(result.response) > 3000:
            doc = BufferedInputFile(
                result.response.encode("utf-8"),
                filename="task_output.txt",
            )
            await callback.message.answer_document(
                doc,
                caption="📄 Полный вывод задачи в файле",
            )


# -----------------------------------------------------------------------------
# Post-execution follow-up handlers (new query / change directory)
# -----------------------------------------------------------------------------


@run_router.callback_query(F.data.startswith("post:same:"))
async def cb_post_execution_same_folder(
    callback: CallbackQuery,
    state: FSMContext,
    confirmation_store: ConfirmationStore,
    config: AppConfig,
) -> None:
    """Prompt for another task in the same workspace directory."""
    if not callback.data:
        return

    op_id = callback.data.split(":", 2)[2]
    op = confirmation_store.get_operation(op_id)
    if op is None or (not op.workspace_path and not op.workspace_alias):
        await callback.answer(
            "Сессия устарела. Выберите директорию заново через /run.",
            show_alert=True,
        )
        return

    target_path = (
        op.workspace_path
        if op.workspace_path
        else config.allowed_workspaces.get(op.workspace_alias)
    )
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if not is_distributed:
        if target_path is None or not target_path.exists() or not target_path.is_dir():
            await callback.answer(
                "Директория недоступна или была удалена.",
                show_alert=True,
            )
            return
    elif target_path is None:
        await callback.answer(
            "Директория недоступна или была удалена.",
            show_alert=True,
        )
        return

    alias = (
        op.workspace_alias
        or ntpath.basename(str(target_path).rstrip("\\/"))
        or str(target_path)
    )
    await state.set_state(RunWizard.waiting_for_prompt)
    await state.update_data(
        selected_path=str(target_path),
        workspace_alias=alias,
    )

    text = (
        f"📁 <b>Рабочая директория сохранена:</b>\n"
        f"<code>{html.escape(str(target_path))}</code>\n\n"
        "💬 <b>Отправьте в чат текст новой команды/задачи для агента:</b>"
    )
    keyboard = _build_prompt_cancel_keyboard()
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@run_router.callback_query(F.data == "post:change")
async def cb_post_execution_change_folder(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Open directory chooser wizard after task execution."""
    await state.clear()
    await state.set_state(RunWizard.choosing_directory)

    text = (
        "📁 <b>Выберите рабочую директорию:</b>\n"
        "Вы можете использовать сохранённые пространства или выбрать диск."
    )
    keyboard = _build_workspace_selection_keyboard(config, agent_dispatcher=agent_dispatcher)
    if isinstance(callback.message, Message):
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()
