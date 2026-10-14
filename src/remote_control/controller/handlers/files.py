"""Interactive Telegram handlers for bidirectional file transfer and filesystem browsing."""

from __future__ import annotations

import asyncio
import html
import logging
import ntpath
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from remote_control.common.protocol import (
    MAX_FILE_DOWNLOAD_BYTES,
    MAX_FILE_UPLOAD_BYTES,
    FileItem,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.browser import (
    format_file_size,
    get_available_drives,
    get_parent_directory,
    is_drive_root,
    list_directory_contents,
)
from remote_control.controller.handlers.run import get_user_active_workspace

logger = logging.getLogger(__name__)

files_router = Router(name="files_router")

_ITEMS_PAGE_SIZE = 8

# Pending upload sessions: transfer_id -> session dict
_pending_uploads: dict[str, dict[str, Any]] = {}


class FileBrowserState(StatesGroup):
    """FSM states for interactive file browsing."""

    browsing = State()


# -----------------------------------------------------------------------------
# Keyboard Builders
# -----------------------------------------------------------------------------


def _build_files_workspaces_keyboard(
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
    upload_transfer_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for initial file browser view (workspaces & drives)."""
    buttons: list[list[InlineKeyboardButton]] = []

    workspaces: dict[str, str] = {}
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        workspaces = agent_dispatcher.get_available_workspaces()
    else:
        workspaces = {k: str(v) for k, v in config.allowed_workspaces.items()}

    for alias, path_val in workspaces.items():
        icon = "🏠" if alias.lower() in ("control", "home", "default") else "📁"
        base_name = ntpath.basename(str(path_val).rstrip("\\/")) or str(path_val)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {alias} ({base_name})",
                    callback_data=f"fb:ws:{alias}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="💾 Выбрать диск...",
                callback_data="fb:drives",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Закрыть",
                callback_data="fb:close",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _build_files_drives_keyboard(
    drives: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard displaying available disk drives."""
    if drives is None:
        drives = await asyncio.to_thread(get_available_drives)

    drive_buttons: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []

    for d in drives:
        letter = d.rstrip(":\\").rstrip("/")
        drive_buttons.append(
            InlineKeyboardButton(
                text=f"💽 Диск {d}",
                callback_data=f"fb:drv:{letter}",
            )
        )

    for i in range(0, len(drive_buttons), 2):
        rows.append(drive_buttons[i : i + 2])

    rows.append(
        [
            InlineKeyboardButton(text="⬅️ К воркспейсам", callback_data="fb:home"),
            InlineKeyboardButton(text="❌ Закрыть", callback_data="fb:close"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_files_folder_keyboard(
    subdirs: list[Path],
    files: list[FileItem],
    page: int,
    current_path: Path,
    upload_transfer_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard displaying subdirectories, downloadable files, and navigation."""
    items: list[tuple[str, Any]] = [("dir", p) for p in subdirs] + [("file", f) for f in files]
    total_items = len(items)
    total_pages = max(1, (total_items + _ITEMS_PAGE_SIZE - 1) // _ITEMS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * _ITEMS_PAGE_SIZE
    end_idx = min(start_idx + _ITEMS_PAGE_SIZE, total_items)
    page_items = items[start_idx:end_idx]

    rows: list[list[InlineKeyboardButton]] = []

    # If upload is pending, offer to save file in current folder
    if upload_transfer_id:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📥 Сохранить полученный файл сюда",
                    callback_data=f"fb:save_here:{upload_transfer_id}",
                )
            ]
        )

    dir_row: list[InlineKeyboardButton] = []
    for offset, (kind, item) in enumerate(page_items):
        item_idx = start_idx + offset
        if kind == "dir":
            folder_name = ntpath.basename(str(item).rstrip("\\/")) or str(item)
            btn = InlineKeyboardButton(
                text=f"📁 {folder_name[:22]}",
                callback_data=f"fb:dir:{item_idx}",
            )
            dir_row.append(btn)
            if len(dir_row) == 2:
                rows.append(dir_row)
                dir_row = []
        else:
            if dir_row:
                rows.append(dir_row)
                dir_row = []
            file_item: FileItem = item
            size_str = format_file_size(file_item.size_bytes)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📄 {file_item.name[:22]} ({size_str})",
                        callback_data=f"fb:dl:{item_idx}",
                    )
                ]
            )
    if dir_row:
        rows.append(dir_row)

    # Pagination controls
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(text="◀️ Пред.", callback_data=f"fb:page:{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(
                text=f"📄 {page + 1}/{total_pages}",
                callback_data="fb:noop",
            )
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(text="След. ▶️", callback_data=f"fb:page:{page + 1}")
            )
        rows.append(nav_row)

    # Navigation up
    up_btn = (
        InlineKeyboardButton(text="⬆️ На уровень вверх", callback_data="fb:up")
        if not is_drive_root(current_path)
        else InlineKeyboardButton(text="⬅️ К выбору диска", callback_data="fb:drives")
    )
    rows.append(
        [
            up_btn,
            InlineKeyboardButton(text="❌ Закрыть", callback_data="fb:close"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_upload_destination_keyboard(
    transfer_id: str,
    user_id: int,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> InlineKeyboardMarkup:
    """Build destination selection keyboard for an incoming document."""
    buttons: list[list[InlineKeyboardButton]] = []

    # 1. Current active workspace (if user worked in one)
    active_ws = get_user_active_workspace(user_id)
    if active_ws:
        ws_name = ntpath.basename(str(active_ws).rstrip("\\/")) or str(active_ws)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🏠 Текущая папка ({ws_name})",
                    callback_data=f"fu:dst:act:{transfer_id}",
                )
            ]
        )

    # 2. Configured workspaces
    workspaces: dict[str, str] = {}
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        workspaces = agent_dispatcher.get_available_workspaces()
    else:
        workspaces = {k: str(v) for k, v in config.allowed_workspaces.items()}

    for alias, path_val in workspaces.items():
        base_name = ntpath.basename(str(path_val).rstrip("\\/")) or str(path_val)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {alias} ({base_name})",
                    callback_data=f"fu:dst:ws:{alias}:{transfer_id}",
                )
            ]
        )

    # 3. Interactive browser picker
    buttons.append(
        [
            InlineKeyboardButton(
                text="📂 Выбрать другую папку...",
                callback_data=f"fu:dst:brw:{transfer_id}",
            )
        ]
    )

    # 4. Cancel
    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"fu:cancel:{transfer_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_conflict_keyboard(transfer_id: str) -> InlineKeyboardMarkup:
    """Build conflict resolution keyboard (Variant B: Overwrite, Suffix, Cancel)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ Перезаписать",
                    callback_data=f"fu:act:ovw:{transfer_id}",
                ),
                InlineKeyboardButton(
                    text="📋 Сохранить с суффиксом (1)",
                    callback_data=f"fu:act:suf:{transfer_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"fu:cancel:{transfer_id}",
                )
            ],
        ]
    )


# -----------------------------------------------------------------------------
# File Fetching / Writing Helpers
# -----------------------------------------------------------------------------


async def _fetch_directory_data(
    path_str: str,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None,
) -> tuple[list[Path], list[FileItem], str | None]:
    """Fetch subdirectories and files for a directory across local or distributed mode."""
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        if not agent_dispatcher.is_agent_connected():
            return [], [], "Windows Agent оффлайн. Включите ПК (/wake) или запустите agent."
        subdirs, files = await agent_dispatcher.request_remote_directory_contents(path_str)
        return subdirs, files, None
    else:
        p = Path(path_str)
        if not p.exists() or not p.is_dir():
            return [], [], f"Каталог не найден на диске: {path_str}"
        subdirs, files = await asyncio.to_thread(list_directory_contents, p)
        return subdirs, files, None


async def _check_file_exists_in_dir(
    target_dir: str,
    filename: str,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None,
) -> bool:
    """Check if a file with given name already exists in target directory."""
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        _, files = await agent_dispatcher.request_remote_directory_contents(target_dir)
        return any(f.name.lower() == filename.lower() for f in files)
    else:
        dest_p = Path(target_dir) / filename
        return dest_p.exists()


async def _save_upload_to_disk(
    session: dict[str, Any],
    target_dir_str: str,
    conflict_mode: str,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None,
) -> tuple[bool, str]:
    """Save uploaded file to local or remote disk according to conflict mode.

    Returns (success, saved_path_or_error_message).
    """
    temp_file: Path = session["temp_file"]
    filename: str = session["filename"]
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"

    try:
        data = await asyncio.to_thread(temp_file.read_bytes)
    except Exception as e:
        return False, f"Ошибка чтения временного файла: {e}"

    if is_distributed and agent_dispatcher is not None:
        try:
            ack = await agent_dispatcher.stage_file_upload(
                target_dir=target_dir_str,
                filename=filename,
                file_bytes=data,
                conflict_mode=conflict_mode,
                timeout_seconds=60.0,
            )
            if ack.success:
                return True, ack.saved_path
            return False, ack.error or "Неизвестная ошибка сохранения на агенте"
        except Exception as e:
            return False, f"Ошибка передачи файла на агент: {e}"
    else:
        try:
            target_dir = Path(target_dir_str)
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_path = target_dir / filename
            if dest_path.exists() and conflict_mode == "suffix":
                stem = dest_path.stem
                suffix = dest_path.suffix
                c = 1
                while dest_path.exists():
                    dest_path = target_dir / f"{stem} ({c}){suffix}"
                    c += 1
            await asyncio.to_thread(dest_path.write_bytes, data)
            return True, str(dest_path)
        except Exception as e:
            return False, f"Ошибка записи файла на диск: {e}"


# -----------------------------------------------------------------------------
# Handlers: /files and /download
# -----------------------------------------------------------------------------


@files_router.message(Command("files"))
async def cmd_files(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Launch interactive filesystem explorer for viewing and downloading files."""
    await state.clear()
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"

    if is_distributed and agent_dispatcher is not None and not agent_dispatcher.is_agent_connected():
        await message.answer(
            "⚠️ <b>Windows Agent оффлайн.</b>\n"
            "Включите ПК (/wake) и убедитесь, что агент запущен.",
            parse_mode="HTML",
        )
        return

    # Check if a specific path was passed as argument: /files C:\path
    raw_path = (command.args or "").strip().strip('"').strip("'")
    if raw_path:
        subdirs, files, err = await _fetch_directory_data(raw_path, config, agent_dispatcher)
        if err:
            await message.answer(f"❌ {html.escape(err)}")
            return

        current_path = Path(raw_path)
        items_payload = [f.model_dump(mode="json") for f in files]
        await state.set_state(FileBrowserState.browsing)
        await state.update_data(
            current_path=raw_path,
            subdirs=[str(p) for p in subdirs],
            files=items_payload,
            page=0,
            upload_transfer_id=None,
        )

        keyboard = _build_files_folder_keyboard(subdirs, files, 0, current_path)
        text = (
            f"📂 <b>Папка:</b> <code>{html.escape(raw_path)}</code>\n"
            f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
            "<i>Нажмите на файл для скачивания на телефон:</i>"
        )
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        return

    # No argument: show workspaces and drives
    await state.set_state(FileBrowserState.browsing)
    await state.update_data(upload_transfer_id=None)
    keyboard = _build_files_workspaces_keyboard(config, agent_dispatcher=agent_dispatcher)
    text = (
        "📂 <b>Проводник файлов и папок</b>\n\n"
        "Выберите рабочее пространство или диск для просмотра файлов и скачивания:"
    )
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@files_router.message(Command("download", "get"))
async def cmd_download(
    message: Message,
    command: CommandObject,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Directly download a file by its Windows absolute path."""
    raw_path = (command.args or "").strip().strip('"').strip("'")
    if not raw_path:
        await message.answer(
            "ℹ️ <b>Использование:</b> <code>/download &lt;путь_к_файлу&gt;</code>\n\n"
            "<i>Пример:</i> <code>/download C:\\Users\\kosty\\report.pdf</code>\n"
            "Или откройте интерактивный проводник командой /files.",
            parse_mode="HTML",
        )
        return

    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None and not agent_dispatcher.is_agent_connected():
        await message.answer(
            "⚠️ <b>Windows Agent оффлайн.</b> Включите ПК (/wake).",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(
        f"⏳ <b>Подготовка файла к отправке:</b>\n<code>{html.escape(raw_path)}</code>",
        parse_mode="HTML",
    )

    if is_distributed and agent_dispatcher is not None:
        try:
            filename, file_bytes = await agent_dispatcher.request_file_download(
                raw_path, timeout_seconds=60.0
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка скачивания файла: {html.escape(str(e))}")
            return
    else:
        p = Path(raw_path)
        if not p.exists() or not p.is_file():
            await status_msg.edit_text(f"❌ Файл не найден на диске: <code>{html.escape(raw_path)}</code>", parse_mode="HTML")
            return
        size = p.stat().st_size
        if size > MAX_FILE_DOWNLOAD_BYTES:
            size_mb = size / (1024 * 1024)
            await status_msg.edit_text(
                f"❌ Файл превышает лимит отправки Telegram (максимум 50 МБ, размер: {size_mb:.1f} МБ)."
            )
            return
        filename = p.name
        file_bytes = await asyncio.to_thread(p.read_bytes)

    try:
        size_display = format_file_size(len(file_bytes))
        await message.answer_document(
            document=BufferedInputFile(file_bytes, filename=filename),
            caption=f"📄 <b>{html.escape(filename)}</b> ({size_display})",
            parse_mode="HTML",
        )
        await status_msg.delete()
    except Exception as e:
        logger.exception("Failed to send document to Telegram: %s", e)
        await status_msg.edit_text(f"❌ Ошибка отправки документа в Telegram: {html.escape(str(e))}")


# -----------------------------------------------------------------------------
# Handlers: Incoming Document Upload (Telegram ➔ PC)
# -----------------------------------------------------------------------------


@files_router.message(F.document)
async def handle_document_upload(
    message: Message,
    bot: Bot,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Receive a document from Telegram and offer destination folders on PC."""
    document = message.document
    if document is None:
        return

    # Check 20 MB Telegram bot download limit
    if document.file_size and document.file_size > MAX_FILE_UPLOAD_BYTES:
        size_mb = document.file_size / (1024 * 1024)
        await message.answer(
            f"❌ <b>Файл слишком большой!</b>\n"
            f"Размер файла: <b>{size_mb:.1f} МБ</b>.\n"
            "Telegram Bot API не позволяет ботам скачивать файлы больше <b>20 МБ</b>.",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(
        f"⏳ Загрузка файла <code>{html.escape(document.file_name or 'file')}</code> с серверов Telegram...",
        parse_mode="HTML",
    )

    temp_dir = Path(tempfile.gettempdir()) / "remote_control_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    transfer_id = uuid.uuid4().hex[:12]
    filename = document.file_name or f"upload_{transfer_id}"
    temp_file = temp_dir / f"{transfer_id}_{filename}"

    try:
        await bot.download(document.file_id, destination=temp_file)
    except Exception as e:
        logger.exception("Failed to download document from Telegram: %s", e)
        await status_msg.edit_text(f"❌ Ошибка скачивания файла из Telegram: {html.escape(str(e))}")
        return

    user_id = message.from_user.id if message.from_user else 0
    _pending_uploads[transfer_id] = {
        "transfer_id": transfer_id,
        "temp_file": temp_file,
        "filename": filename,
        "file_size": document.file_size or 0,
        "user_id": user_id,
        "chat_id": message.chat.id,
    }

    size_display = format_file_size(document.file_size or 0)
    keyboard = _build_upload_destination_keyboard(
        transfer_id=transfer_id,
        user_id=user_id,
        config=config,
        agent_dispatcher=agent_dispatcher,
    )
    text = (
        f"📥 <b>Получен файл:</b> <code>{html.escape(filename)}</code> ({size_display})\n\n"
        "Куда сохранить файл на компьютере?"
    )
    await status_msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


# -----------------------------------------------------------------------------
# Callbacks: File Upload Destination & Conflict Resolution
# -----------------------------------------------------------------------------


@files_router.callback_query(F.data.startswith("fu:dst:act:"))
async def cb_upload_dest_active(
    callback: CallbackQuery,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Save incoming file to currently active workspace."""
    transfer_id = callback.data.split(":", 3)[3]
    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else 0
    active_ws = get_user_active_workspace(user_id)
    if not active_ws:
        await callback.answer("Активное рабочее пространство не выбрано.", show_alert=True)
        return

    await _process_destination_choice(
        callback, session, str(active_ws), config, agent_dispatcher
    )


@files_router.callback_query(F.data.startswith("fu:dst:ws:"))
async def cb_upload_dest_workspace(
    callback: CallbackQuery,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Save incoming file to chosen workspace alias."""
    parts = callback.data.split(":", 4)
    alias = parts[3]
    transfer_id = parts[4]

    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    target_dir_str = None
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        workspaces = agent_dispatcher.get_available_workspaces()
        if alias in workspaces:
            target_dir_str = workspaces[alias]

    if not target_dir_str:
        if alias in config.allowed_workspaces:
            target_dir_str = str(config.allowed_workspaces[alias])

    if not target_dir_str:
        await callback.answer("Рабочее пространство не найдено.", show_alert=True)
        return

    await _process_destination_choice(
        callback, session, target_dir_str, config, agent_dispatcher
    )


@files_router.callback_query(F.data.startswith("fu:dst:brw:"))
async def cb_upload_dest_browse(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Open file browser so user can select custom destination directory."""
    transfer_id = callback.data.split(":", 3)[3]
    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    await state.set_state(FileBrowserState.browsing)
    await state.update_data(upload_transfer_id=transfer_id)

    keyboard = _build_files_workspaces_keyboard(
        config, agent_dispatcher=agent_dispatcher, upload_transfer_id=transfer_id
    )
    text = (
        f"📂 <b>Выбор папки для сохранения файла:</b>\n"
        f"<code>{html.escape(session['filename'])}</code>\n\n"
        "Выберите рабочее пространство или диск:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:save_here:"))
async def cb_save_here(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Save pending upload into currently opened directory in browser."""
    transfer_id = callback.data.split(":", 2)[2]
    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    data = await state.get_data()
    current_path = data.get("current_path")
    if not current_path:
        await callback.answer("Директория не определена.", show_alert=True)
        return

    await _process_destination_choice(
        callback, session, current_path, config, agent_dispatcher
    )


async def _process_destination_choice(
    callback: CallbackQuery,
    session: dict[str, Any],
    target_dir_str: str,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None,
) -> None:
    """Check for file conflict in destination, show conflict prompt or save."""
    transfer_id = session["transfer_id"]
    filename = session["filename"]
    session["target_dir"] = target_dir_str

    # Check if file exists in target directory
    exists = await _check_file_exists_in_dir(
        target_dir_str, filename, config, agent_dispatcher
    )

    if exists:
        # Show Variant B buttons: Overwrite, Suffix (1), Cancel
        keyboard = _build_conflict_keyboard(transfer_id)
        text = (
            f"⚠️ <b>Файл уже существует!</b>\n\n"
            f"Файл <code>{html.escape(filename)}</code> уже найден в папке:\n"
            f"<code>{html.escape(target_dir_str)}</code>\n\n"
            "Выберите действие:"
        )
        if isinstance(callback.message, Message):
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
        return

    # No conflict: save immediately
    await _finalize_upload(callback, session, "overwrite", config, agent_dispatcher)


@files_router.callback_query(F.data.startswith("fu:act:ovw:"))
async def cb_conflict_overwrite(
    callback: CallbackQuery,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Overwrite existing file."""
    transfer_id = callback.data.split(":", 3)[3]
    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    await _finalize_upload(callback, session, "overwrite", config, agent_dispatcher)


@files_router.callback_query(F.data.startswith("fu:act:suf:"))
async def cb_conflict_suffix(
    callback: CallbackQuery,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Save file with numerical suffix (1)."""
    transfer_id = callback.data.split(":", 3)[3]
    session = _pending_uploads.get(transfer_id)
    if not session:
        await callback.answer("Сессия загрузки файла истекла.", show_alert=True)
        return

    await _finalize_upload(callback, session, "suffix", config, agent_dispatcher)


@files_router.callback_query(F.data.startswith("fu:cancel:"))
async def cb_upload_cancel(
    callback: CallbackQuery,
) -> None:
    """Cancel file upload and delete temp file."""
    transfer_id = callback.data.split(":", 2)[2]
    session = _pending_uploads.pop(transfer_id, None)
    if session and session.get("temp_file"):
        try:
            p: Path = session["temp_file"]
            if p.exists():
                p.unlink()
        except OSError:
            pass

    if isinstance(callback.message, Message):
        await callback.message.edit_text("❌ Загрузка файла отменена.")
    await callback.answer("Загрузка отменена.")


async def _finalize_upload(
    callback: CallbackQuery,
    session: dict[str, Any],
    conflict_mode: str,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None,
) -> None:
    """Complete upload by saving to disk and cleaning up session."""
    transfer_id = session["transfer_id"]
    target_dir_str = session.get("target_dir", "")
    filename = session["filename"]

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"⏳ Сохранение <code>{html.escape(filename)}</code> на ПК...",
            parse_mode="HTML",
        )

    success, result_info = await _save_upload_to_disk(
        session=session,
        target_dir_str=target_dir_str,
        conflict_mode=conflict_mode,
        config=config,
        agent_dispatcher=agent_dispatcher,
    )

    # Clean up temp file
    temp_file: Path = session["temp_file"]
    try:
        if temp_file.exists():
            temp_file.unlink()
    except OSError:
        pass
    _pending_uploads.pop(transfer_id, None)

    if success:
        text = (
            "✅ <b>Файл успешно сохранён на ПК!</b>\n\n"
            f"📁 <b>Путь:</b> <code>{html.escape(result_info)}</code>\n"
            f"📊 <b>Размер:</b> {format_file_size(session.get('file_size', 0))}"
        )
    else:
        text = f"❌ <b>Ошибка при сохранении файла:</b>\n{html.escape(result_info)}"

    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


# -----------------------------------------------------------------------------
# Callbacks: File Browser Navigation & Download
# -----------------------------------------------------------------------------


@files_router.callback_query(F.data == "fb:home")
async def cb_files_home(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Return to workspaces view in file browser."""
    data = await state.get_data()
    upload_id = data.get("upload_transfer_id")
    keyboard = _build_files_workspaces_keyboard(
        config, agent_dispatcher=agent_dispatcher, upload_transfer_id=upload_id
    )
    text = (
        "📂 <b>Проводник файлов и папок</b>\n\n"
        "Выберите рабочее пространство или диск:"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:ws:"))
async def cb_files_select_workspace(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Open chosen workspace folder in file browser."""
    alias = callback.data.split(":", 2)[2]
    target_path = None
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"

    if is_distributed and agent_dispatcher is not None:
        workspaces = agent_dispatcher.get_available_workspaces()
        if alias in workspaces:
            target_path = Path(workspaces[alias])

    if target_path is None and alias in config.allowed_workspaces:
        target_path = config.allowed_workspaces[alias]

    if target_path is None:
        await callback.answer("Рабочее пространство не найдено.", show_alert=True)
        return

    subdirs, files, err = await _fetch_directory_data(str(target_path), config, agent_dispatcher)
    if err:
        await callback.answer(err, show_alert=True)
        return

    data = await state.get_data()
    upload_id = data.get("upload_transfer_id")
    await state.update_data(
        current_path=str(target_path),
        subdirs=[str(p) for p in subdirs],
        files=[f.model_dump(mode="json") for f in files],
        page=0,
    )

    keyboard = _build_files_folder_keyboard(
        subdirs, files, 0, target_path, upload_transfer_id=upload_id
    )
    text = (
        f"📂 <b>Папка:</b> <code>{html.escape(str(target_path))}</code> "
        f"(<b>{html.escape(alias)}</b>)\n"
        f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
        "<i>Нажмите на файл для скачивания на телефон:</i>"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data == "fb:drives")
async def cb_files_show_drives(
    callback: CallbackQuery,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Show system disks in file browser."""
    is_distributed = getattr(config, "controller_mode", "local") == "distributed"
    if is_distributed and agent_dispatcher is not None:
        if not agent_dispatcher.is_agent_connected():
            await callback.answer("⚠️ Windows Agent оффлайн.", show_alert=True)
            return
        drives = agent_dispatcher.get_available_drives()
    else:
        drives = await asyncio.to_thread(get_available_drives)

    keyboard = await _build_files_drives_keyboard(drives)
    text = "💾 <b>Выберите системный диск:</b>"
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:drv:"))
async def cb_files_select_drive(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Open root of selected drive in file browser."""
    letter = callback.data.split(":", 2)[2]
    drive_str = f"{letter}:\\"
    drive_path = Path(drive_str)

    subdirs, files, err = await _fetch_directory_data(drive_str, config, agent_dispatcher)
    if err:
        await callback.answer(err, show_alert=True)
        return

    data = await state.get_data()
    upload_id = data.get("upload_transfer_id")
    await state.update_data(
        current_path=drive_str,
        subdirs=[str(p) for p in subdirs],
        files=[f.model_dump(mode="json") for f in files],
        page=0,
    )

    keyboard = _build_files_folder_keyboard(
        subdirs, files, 0, drive_path, upload_transfer_id=upload_id
    )
    text = (
        f"📂 <b>Диск:</b> <code>{html.escape(drive_str)}</code>\n"
        f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
        "<i>Нажмите на файл для скачивания на телефон:</i>"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:dir:"))
async def cb_files_open_dir(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Navigate into a chosen subdirectory."""
    idx = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    subdirs_raw = data.get("subdirs", [])
    if idx < 0 or idx >= len(subdirs_raw):
        await callback.answer("Папка не найдена в списке.", show_alert=True)
        return

    target_dir_str = subdirs_raw[idx]
    target_dir = Path(target_dir_str)

    subdirs, files, err = await _fetch_directory_data(target_dir_str, config, agent_dispatcher)
    if err:
        await callback.answer(err, show_alert=True)
        return

    upload_id = data.get("upload_transfer_id")
    await state.update_data(
        current_path=target_dir_str,
        subdirs=[str(p) for p in subdirs],
        files=[f.model_dump(mode="json") for f in files],
        page=0,
    )

    keyboard = _build_files_folder_keyboard(
        subdirs, files, 0, target_dir, upload_transfer_id=upload_id
    )
    text = (
        f"📂 <b>Папка:</b> <code>{html.escape(target_dir_str)}</code>\n"
        f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
        "<i>Нажмите на файл для скачивания на телефон:</i>"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data == "fb:up")
async def cb_files_up(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Navigate to parent directory."""
    data = await state.get_data()
    current_path_str = data.get("current_path")
    if not current_path_str:
        await cb_files_home(callback, state, config, agent_dispatcher)
        return

    parent = get_parent_directory(current_path_str)
    if parent is None:
        await cb_files_show_drives(callback, config, agent_dispatcher)
        return

    parent_str = str(parent)
    subdirs, files, err = await _fetch_directory_data(parent_str, config, agent_dispatcher)
    if err:
        await callback.answer(err, show_alert=True)
        return

    upload_id = data.get("upload_transfer_id")
    await state.update_data(
        current_path=parent_str,
        subdirs=[str(p) for p in subdirs],
        files=[f.model_dump(mode="json") for f in files],
        page=0,
    )

    keyboard = _build_files_folder_keyboard(
        subdirs, files, 0, parent, upload_transfer_id=upload_id
    )
    text = (
        f"📂 <b>Папка:</b> <code>{html.escape(parent_str)}</code>\n"
        f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
        "<i>Нажмите на файл для скачивания на телефон:</i>"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:page:"))
async def cb_files_page(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Switch page in files browser."""
    page = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    current_path_str = data.get("current_path", "")
    subdirs_raw = data.get("subdirs", [])
    files_raw = data.get("files", [])
    upload_id = data.get("upload_transfer_id")

    subdirs = [Path(p) for p in subdirs_raw]
    files = [FileItem.model_validate(f) for f in files_raw]
    await state.update_data(page=page)

    keyboard = _build_files_folder_keyboard(
        subdirs, files, page, Path(current_path_str), upload_transfer_id=upload_id
    )
    text = (
        f"📂 <b>Папка:</b> <code>{html.escape(current_path_str)}</code>\n"
        f"<i>Папок: {len(subdirs)}, Файлов: {len(files)}</i>\n\n"
        "<i>Нажмите на файл для скачивания на телефон:</i>"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@files_router.callback_query(F.data.startswith("fb:dl:"))
async def cb_files_download(
    callback: CallbackQuery,
    state: FSMContext,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Handle click on a file button to download file from PC to Telegram."""
    idx = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    current_path_str = data.get("current_path")
    files_raw = data.get("files", [])
    subdirs_raw = data.get("subdirs", [])

    file_offset = idx - len(subdirs_raw)
    if file_offset < 0 or file_offset >= len(files_raw):
        await callback.answer("Файл не найден.", show_alert=True)
        return

    file_dict = files_raw[file_offset]
    file_item = FileItem.model_validate(file_dict)

    if current_path_str:
        file_path_str = str(Path(current_path_str) / file_item.name)
    else:
        file_path_str = file_item.name

    if file_item.size_bytes > MAX_FILE_DOWNLOAD_BYTES:
        size_mb = file_item.size_bytes / (1024 * 1024)
        await callback.answer(
            f"Файл слишком велик (> 50 МБ): {size_mb:.1f} МБ.",
            show_alert=True,
        )
    target_msg = callback.message if isinstance(callback.message, Message) else None
    if not target_msg:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return

    await callback.answer("⏳ Скачивание файла...")

    is_distributed = getattr(config, "controller_mode", "local") == "distributed"

    if is_distributed and agent_dispatcher is not None:
        try:
            filename, file_bytes = await agent_dispatcher.request_file_download(
                file_path_str, timeout_seconds=60.0
            )
        except Exception as e:
            await target_msg.answer(f"❌ Ошибка скачивания файла: {html.escape(str(e))}")
            return
    else:
        p = Path(file_path_str)
        if not p.exists() or not p.is_file():
            await target_msg.answer(f"❌ Файл не найден: <code>{html.escape(file_path_str)}</code>")
            return
        filename = p.name
        file_bytes = await asyncio.to_thread(p.read_bytes)

    try:
        size_display = format_file_size(len(file_bytes))
        await target_msg.answer_document(
            document=BufferedInputFile(file_bytes, filename=filename),
            caption=f"📄 <b>{html.escape(filename)}</b> ({size_display})",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Failed to send document to Telegram: %s", e)
        await target_msg.answer(f"❌ Ошибка отправки документа в Telegram: {html.escape(str(e))}")



@files_router.callback_query(F.data == "fb:noop")
async def cb_files_noop(callback: CallbackQuery) -> None:
    """Acknowledge no-op page indicator button."""
    await callback.answer()


@files_router.callback_query(F.data == "fb:close")
async def cb_files_close(callback: CallbackQuery, state: FSMContext) -> None:
    """Close interactive file browser."""
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.delete()
    await callback.answer("Проводник закрыт.")
