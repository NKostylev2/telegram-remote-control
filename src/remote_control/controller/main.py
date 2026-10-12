"""Standard CLI entry point for running Telegram Remote Control bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeDefault

from remote_control.agent.agy_client import AgyClient
from remote_control.config import AppConfig, load_config
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.bot import create_bot, create_dispatcher
from remote_control.controller.server import ControllerServer
from remote_control.controller.single_instance import SingleInstanceError, SingleInstanceLock
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import TaskStore

logger = logging.getLogger("remote_control")


async def setup_bot_commands(bot: Bot) -> None:
    """Register menu commands with Telegram Bot API for the native Menu button."""
    commands = [
        BotCommand(command="run", description="🚀 Запустить новую задачу"),
        BotCommand(command="screenshot", description="📸 Снимок экрана рабочего стола"),
        BotCommand(command="tasks", description="📋 Очередь и история задач"),
        BotCommand(command="status", description="📊 Состояние хоста и CLI"),
        BotCommand(command="workspace", description="📁 Выбор рабочей папки"),
        BotCommand(command="cancel", description="🛑 Отменить задачу"),
        BotCommand(command="result", description="📄 Получить результат задачи"),
        BotCommand(command="help", description="❓ Справка по командам"),
        BotCommand(command="start", description="🏠 Главное меню"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Меню команд Telegram успешно зарегистрировано.")
    except Exception as e:
        logger.warning("Не удалось зарегистрировать меню команд Telegram: %s", e)


async def run_bot(config: AppConfig | None = None) -> None:
    """Launch the Telegram bot in Long Polling mode with graceful shutdown."""
    if config is None:
        try:
            config = load_config()
        except Exception as e:
            logger.error("Ошибка загрузки конфигурации: %s", e)
            sys.exit(1)

    if config.telegram_bot_token is None:
        logger.error(
            "Критическая ошибка запуска: TELEGRAM_BOT_TOKEN не задан в файле .env или окружении. "
            "Бот не может запуститься."
        )
        sys.exit(1)

    if not config.allowed_telegram_users:
        logger.warning(
            "Внимание: ALLOWED_TELEGRAM_USERS пуст. Все входящие сообщения будут игнорироваться."
        )

    # 1. Single-instance process lock to prevent concurrent database access
    lock_path = config.sqlite_database_path.with_suffix(".lock")
    instance_lock = SingleInstanceLock(lock_path)
    try:
        instance_lock.acquire()
    except SingleInstanceError as e:
        logger.error("Критическая ошибка запуска: %s", e)
        sys.exit(1)

    # 2. Initialize database and recover state on startup
    task_store = TaskStore(
        database_path=config.sqlite_database_path,
        results_dir=config.task_results_dir,
    )
    try:
        task_store.initialize_sync()
        task_store.recover_on_startup_sync()
    except Exception as e:
        logger.error("Ошибка инициализации базы данных задач: %s", e)
        instance_lock.release()
        sys.exit(1)

    # 3. Optional HTTP API server for distributed mode
    agent_dispatcher = None
    server = None
    if config.controller_mode == "distributed":
        agent_dispatcher = AgentDispatcher(config)
        server = ControllerServer(config, agent_dispatcher)
        await server.start()

    # 4. Create bot, client, queue worker, and dispatcher
    bot = create_bot(config)
    agy_client = AgyClient(config)
    dp = create_dispatcher(
        config=config,
        agy_client=agy_client,
        task_store=task_store,
        agent_dispatcher=agent_dispatcher,
    )
    task_queue: TaskQueue = dp["task_queue"]
    task_queue.set_bot(bot)

    logger.info("Инициализация завершена. Запуск очереди задач и Long Polling...")
    task_queue.start()

    try:
        await setup_bot_commands(bot)
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=True,
        )
    except TelegramUnauthorizedError:
        logger.error(
            "Ошибка авторизации Telegram: указан неверный токен TELEGRAM_BOT_TOKEN. "
            "Проверьте токен, полученный от @BotFather в файле .env."
        )
        sys.exit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Получен сигнал завершения работы.")
    except Exception as e:
        logger.error("Непредвиденная ошибка при работе Telegram бота: %s", e)
        sys.exit(1)
    finally:
        await task_queue.stop()
        if server is not None:
            await server.stop()
        instance_lock.release()
        await bot.session.close()
        logger.info("Сессия Telegram Bot закрыта. Завершение работы.")


def main() -> None:
    """Main CLI entry point for console script remote-control-bot."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(run_bot())
    except SystemExit:
        raise
    except Exception as e:
        logger.critical("Неперехваченная ошибка при работе бота: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
