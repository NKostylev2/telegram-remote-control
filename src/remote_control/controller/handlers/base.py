"""Base informational and navigation command handlers."""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from remote_control.agent.agy_client import AgyClient
from remote_control.common.wol import (
    check_host_availability,
    send_wake_on_lan,
    wait_for_host,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.keyboards import get_main_reply_keyboard
from remote_control.controller.task_store import TaskStore

logger = logging.getLogger(__name__)

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
        "• /result <id> — Просмотр сохранённого результата задачи по ID\n"
        "• /wake — Включение компьютера по сети Wake-on-LAN\n\n"
        "💡 Вы также можете просто отправлять текст задачи в чат — "
        "бот предложит подтвердить её и поставит в очередь!"
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard())


@base_router.message(Command("status"))
@base_router.message(F.text.in_(["📊 Статус", "Статус"]))
async def cmd_status(
    message: Message,
    config: AppConfig,
    agy_client: AgyClient,
    agent_dispatcher: AgentDispatcher | None = None,
    task_store: TaskStore | None = None,
) -> None:
    """Display process state, CLI presence, config presence, and workspace count."""
    token_status = "🟢 Задан" if config.telegram_bot_token is not None else "🔴 Не задан"
    mac_status = "🟢 Задан" if config.target_mac_address is not None else "⚪ Не задан"
    users_count = len(config.allowed_telegram_users)
    workspaces_count = len(config.allowed_workspaces)

    mode = getattr(config, "controller_mode", "local")
    if mode == "distributed":
        is_online = agent_dispatcher.is_agent_connected() if agent_dispatcher else False
        agent_info = agent_dispatcher.get_agent_info() if agent_dispatcher else None

        if is_online and agent_info:
            cli_info = (
                f"🟢 Доступен ({agent_info.cli_version})"
                if agent_info.cli_available
                else "🔴 Не найден"
            )
            host_info = (
                f"<code>{html.escape(agent_info.hostname)}</code> "
                f"({html.escape(agent_info.platform)})"
            )
            active_task = (
                f"#{agent_info.current_task_id}" if agent_info.current_task_id else "нет"
            )
            agent_section = (
                "• Windows Agent: 🟢 Онлайн (подключён)\n"
                f"  - Компьютер: {host_info}\n"
                f"  - Antigravity CLI: {cli_info}\n"
                f"  - Активная задача: {active_task}\n"
            )
        else:
            agent_section = (
                "• Windows Agent: 🔴 Оффлайн (не подключён)\n"
                "  <i>💡 Включите компьютер через команду /wake</i>\n"
            )
        mode_section = (
            "• Режим работы: Распределённый Controller "
            "(Android Controller + Windows Agent)"
        )
    else:
        cli_status = "🟢 Доступен" if agy_client.is_cli_available() else "🔴 Не найден"
        agent_section = (
            f"• Antigravity CLI: {cli_status}\n"
            "ℹ️ Целевой ПК и Windows Agent выполняются локально в одной среде.\n"
        )
        mode_section = (
            "• Режим работы: Локальный Controller "
            "(без отдельного удалённого Windows Agent)"
        )

    queue_info = ""
    if task_store is not None:
        try:
            q_cnt = await task_store.get_queued_count()
            queue_info = f"• Задач в очереди: {q_cnt}\n"
        except Exception:
            pass

    text = (
        "🖥️ Статус системы (Telegram Remote Control)\n\n"
        "• Процесс бота: 🟢 Работает\n"
        f"{mode_section}\n"
        f"{agent_section}"
        f"{queue_info}"
        f"• Разрешённых рабочих пространств: {workspaces_count}\n\n"
        "Параметры конфигурации:\n"
        f"• TELEGRAM_BOT_TOKEN: {token_status}\n"
        f"• ALLOWED_TELEGRAM_USERS: {users_count} пользователей\n"
        f"• TARGET_MAC_ADDRESS: {mac_status}\n"
        f"• CONTROLLER_MODE: {mode}\n"
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard())


@base_router.message(Command("workspace"))
@base_router.message(F.text.in_(["📁 Рабочая папка", "Рабочая папка", "📁 Каталоги", "Каталоги"]))
async def cmd_workspace(
    message: Message,
    config: AppConfig,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Display allowed workspace aliases and their availability without full paths."""
    if not config.allowed_workspaces:
        await message.answer("⚠️ Список разрешённых рабочих пространств пуст в конфигурации.")
        return

    lines = ["📁 <b>Разрешённые рабочие пространства:</b>\n"]

    if getattr(config, "controller_mode", "local") == "distributed":
        agent_online = agent_dispatcher.is_agent_connected() if agent_dispatcher else False
        agent_workspaces = (
            agent_dispatcher.get_available_workspaces()
            if agent_dispatcher
            else {}
        )
        for alias in config.allowed_workspaces.keys():
            if agent_online:
                state_icon = "🟢 Доступно" if alias in agent_workspaces else "⚪ Настроено"
            else:
                state_icon = "⚪ Настроено (Windows Agent оффлайн)"
            lines.append(f"• <b>{html.escape(alias)}</b>: {state_icon}")

        if not agent_online:
            lines.append(
                "\n<i>💡 Windows Agent выключен. Используйте /wake для пробуждения ПК.</i>"
            )
    else:
        for alias, path in config.allowed_workspaces.items():
            is_accessible = path.exists() and path.is_dir()
            state_icon = "🟢 Доступно" if is_accessible else "🔴 Каталог не найден"
            lines.append(f"• <b>{html.escape(alias)}</b>: {state_icon}")

    lines.append(
        "\nДля запуска задачи используйте: <code>/run &lt;псевдоним&gt; &lt;текст&gt;</code>"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@base_router.message(Command("wake"))
@base_router.message(F.text.in_(["⚡ Включить ПК", "Включить ПК"]))
async def cmd_wake(
    message: Message,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    """Send Wake-on-LAN Magic Packet to configured target PC and report status progression."""
    if config is None or not config.target_mac_address:
        await message.answer(
            "⚠️ <b>MAC-адрес целевого ПК не настроен</b>\n\n"
            "Для использования функции Wake-on-LAN укажите MAC-адрес сетевого адаптера "
            "в файле <code>.env</code>:\n"
            "<code>TARGET_MAC_ADDRESS=AA:BB:CC:DD:EE:FF</code>\n\n"
            "Также можно настроить широковещательный IP и порт:\n"
            "<code>WOL_BROADCAST_IP=255.255.255.255</code>\n"
            "<code>WOL_PORT=9</code>",
            parse_mode="HTML",
        )
        return

    escaped_host = html.escape(config.controller_host)
    escaped_mac = html.escape(config.target_mac_address)

    mode = getattr(config, "controller_mode", "local")

    # In distributed mode, check if agent is already connected
    if mode == "distributed" and agent_dispatcher is not None:
        if agent_dispatcher.is_agent_connected():
            await message.answer(
                "🟢 <b>Целевой компьютер уже включён и доступен!</b>\n"
                "Windows Agent подключён и готов к приёму задач.",
                parse_mode="HTML",
            )
            return
    else:
        # In local mode, check if host port is already online
        is_already_online = await check_host_availability(
            config.controller_host, config.controller_port, timeout=1.0
        )
        if is_already_online:
            await message.answer(
                "🟢 <b>Целевой компьютер уже включён и доступен!</b>\n"
                f"Хост <code>{escaped_host}:{config.controller_port}</code> онлайн.",
                parse_mode="HTML",
            )
            return

    status_msg = await message.answer(
        f"📡 <b>Отправляю Wake-on-LAN Magic Packet</b> на MAC: <code>{escaped_mac}</code>...",
        parse_mode="HTML",
    )

    try:
        sent = await send_wake_on_lan(
            mac=config.target_mac_address,
            broadcast_ip=config.wol_broadcast_ip,
            port=config.wol_port,
        )
        await status_msg.edit_text(
            f"📡 <b>Wake-on-LAN Magic Packet отправлен</b> "
            f"({sent} шт. на <code>{escaped_mac}</code>).\n"
            "⏳ Ожидание пробуждения компьютера...",
            parse_mode="HTML",
        )

        # In distributed mode, wait for agent online event with timeout
        if mode == "distributed" and agent_dispatcher is not None:
            agent_online = await agent_dispatcher.wait_for_agent_online(timeout_seconds=60.0)
            if agent_online:
                await status_msg.edit_text(
                    "🟢 <b>Компьютер успешно проснулся и готов к работе!</b>\n"
                    "Windows Agent подключился к Android Controller.",
                    parse_mode="HTML",
                )
                return
            else:
                await status_msg.edit_text(
                    "⏱️ <b>Таймаут ожидания пробуждения (60 сек).</b>\n\n"
                    f"Magic Packet был отправлен на <code>{escaped_mac}</code>, "
                    "но Windows Agent не подключился к серверу за 60 секунд.\n"
                    "<i>Проверьте настройки сетевой карты, BIOS/UEFI "
                    "(Wake on Magic Packet / ErP Ready) и автозапуск служб.</i>",
                    parse_mode="HTML",
                )
                return

        # Local mode: Wait for host with non-blocking polling
        is_online = await wait_for_host(
            config.controller_host,
            config.controller_port,
            max_wait_seconds=60.0,
            initial_delay=1.0,
            check_interval=2.0,
        )

        if is_online:
            if mode == "distributed":
                await status_msg.edit_text(
                    "⚠️ <b>Компьютер включился (сеть доступна), но Windows Agent "
                    "пока не подключился.</b>\n"
                    "Проверьте, запущена ли служба <code>remote-control-agent</code> на Windows.",
                    parse_mode="HTML",
                )
            else:
                await status_msg.edit_text(
                    "🟢 <b>Компьютер успешно проснулся и готов к работе!</b>\n"
                    f"Хост <code>{escaped_host}:{config.controller_port}</code> доступен.",
                    parse_mode="HTML",
                )
        else:
            await status_msg.edit_text(
                "⏱️ <b>Таймаут ожидания пробуждения (60 сек).</b>\n\n"
                f"Magic Packet был отправлен на <code>{escaped_mac}</code>, "
                f"но хост пока не ответил на порту {config.controller_port}.\n"
                "<i>Проверьте настройки сетевой карты и BIOS/UEFI "
                "(Wake on Magic Packet / ErP Ready).</i>",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.exception("Error executing /wake: %s", e)
        await status_msg.edit_text(
            f"❌ <b>Ошибка при отправке Wake-on-LAN:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )

