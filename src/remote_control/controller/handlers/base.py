"""Base informational and navigation command handlers."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from remote_control.agent.agy_client import AgyClient
from remote_control.config import AppConfig
from remote_control.controller.keyboards import get_main_reply_keyboard

base_router = Router(name="base_router")


@base_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Send welcome message and brief purpose explanation with persistent reply keyboard."""
    text = (
        "👋 Привет! Это бот защищённого удалённого управления "
        "компьютером Telegram Remote Control.\n\n"
        "Бот предназначен для:\n"
        "• Мониторинга доступности рабочего окружения на Windows.\n"
        "• Просмотра разрешённых рабочих пространств.\n"
        "• Запуска задач Antigravity CLI с очередью задач и подтверждением.\n"
        "• Получения снимков экрана рабочего стола.\n\n"
        "Используйте кнопки на панели ниже или введите /help для справки."
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard())


@base_router.message(Command("help"))
@base_router.message(F.text.in_(["❓ Помощь", "Помощь", "Справка"]))
async def cmd_help(message: Message) -> None:
    """Send list of currently available and planned commands."""
    text = (
        "📋 Доступные команды в текущей версии:\n\n"
        "• /start — Приветствие и главное меню\n"
        "• /help — Список команд и справка\n"
        "• /run — Интерактивный запуск задачи Antigravity CLI\n"
        "• /screenshot — Сделать снимок экрана рабочего стола\n"
        "• /tasks — Просмотр очереди и истории последних задач\n"
        "• /status — Состояние процесса, доступность CLI и параметры\n"
        "• /workspace — Список разрешённых рабочих пространств\n"
        "• /cancel <id> — Отмена задачи по ID\n"
        "• /result <id> — Просмотр сохранённого результата задачи по ID\n\n"
        "💡 Вы также можете просто отправлять текст задачи в чат — "
        "бот предложит подтвердить её и поставит в очередь!\n\n"
        "⏳ Запланировано в будущих этапах:\n"
        "• /wake — Включение компьютера по сети Wake-on-LAN (Этап 6)"
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard())


@base_router.message(Command("status"))
@base_router.message(F.text.in_(["📊 Статус", "Статус"]))
async def cmd_status(
    message: Message,
    config: AppConfig,
    agy_client: AgyClient,
) -> None:
    """Display process state, CLI presence, config presence, and workspace count."""
    cli_status = "🟢 Доступен" if agy_client.is_cli_available() else "🔴 Не найден"
    token_status = "🟢 Задан" if config.telegram_bot_token is not None else "🔴 Не задан"
    mac_status = "🟢 Задан" if config.target_mac_address is not None else "⚪ Не задан"
    users_count = len(config.allowed_telegram_users)
    workspaces_count = len(config.allowed_workspaces)

    text = (
        "🖥️ Статус системы (Telegram Remote Control)\n\n"
        f"• Процесс бота: 🟢 Работает\n"
        f"• Режим работы: Локальный Controller (без отдельного удалённого Windows Agent)\n"
        f"• Antigravity CLI: {cli_status}\n"
        f"• Разрешённых рабочих пространств: {workspaces_count}\n\n"
        "Параметры конфигурации:\n"
        f"• TELEGRAM_BOT_TOKEN: {token_status}\n"
        f"• ALLOWED_TELEGRAM_USERS: {users_count} пользователей\n"
        f"• TARGET_MAC_ADDRESS: {mac_status}\n"
        f"• CONTROLLER_MODE: {config.controller_mode}\n\n"
        "ℹ️ Целевой ПК и Windows Agent выполняются локально в одной среде."
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard())


@base_router.message(Command("workspace"))
@base_router.message(F.text.in_(["📁 Рабочая папка", "Рабочая папка", "📁 Каталоги", "Каталоги"]))
async def cmd_workspace(message: Message, config: AppConfig) -> None:
    """Display allowed workspace aliases and their availability without full paths."""
    if not config.allowed_workspaces:
        await message.answer("⚠️ Список разрешённых рабочих пространств пуст в конфигурации.")
        return

    lines = ["📁 <b>Разрешённые рабочие пространства:</b>\n"]
    for alias, path in config.allowed_workspaces.items():
        is_accessible = path.exists() and path.is_dir()
        state_icon = "🟢 Доступно" if is_accessible else "🔴 Каталог не найден"
        lines.append(f"• <b>{html.escape(alias)}</b>: {state_icon}")

    lines.append(
        "\nДля запуска задачи используйте: <code>/run &lt;псевдоним&gt; &lt;текст&gt;</code>"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@base_router.message(Command("wake"))
async def cmd_wake(message: Message) -> None:
    """Inform user that Wake-on-LAN is not implemented in current stage."""
    await message.answer(
        "⏳ Функция Wake-on-LAN (/wake) запланирована на Этапе 6 дорожной карты "
        "и в текущей версии не активна."
    )
