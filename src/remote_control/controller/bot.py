"""Bot setup, dispatcher configuration, and error handling for Telegram Remote Control."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from remote_control.agent.agy_client import AgyClient
from remote_control.common.executor import LocalTaskExecutor, RemoteTaskExecutor, TaskExecutor
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.confirmation import ConfirmationStore
from remote_control.controller.handlers import main_router
from remote_control.controller.middlewares import AuthorizationMiddleware
from remote_control.controller.task_queue import TaskQueue
from remote_control.controller.task_store import TaskStore

logger = logging.getLogger(__name__)


def create_bot(config: AppConfig) -> Bot:
    """Create and configure aiogram Bot instance from AppConfig.

    Raises:
        ValueError: If TELEGRAM_BOT_TOKEN is not configured.
    """
    if config.telegram_bot_token is None:
        raise ValueError("Cannot initialize Bot: TELEGRAM_BOT_TOKEN is missing in configuration.")

    token = config.telegram_bot_token.get_secret_value()
    session = None
    if config.telegram_proxy:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=config.telegram_proxy)

    return Bot(token=token, session=session)


def create_dispatcher(
    config: AppConfig,
    agy_client: AgyClient | None = None,
    confirmation_store: ConfirmationStore | None = None,
    task_lock: asyncio.Lock | None = None,
    task_store: TaskStore | None = None,
    task_queue: TaskQueue | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
    executor: TaskExecutor | None = None,
) -> Dispatcher:
    """Create and configure Dispatcher with middlewares, handlers, and dependency injection."""
    dp = Dispatcher()

    client = agy_client or AgyClient(config)
    dispatcher = agent_dispatcher or (
        AgentDispatcher(config) if config.controller_mode == "distributed" else None
    )

    if executor is None:
        if config.controller_mode == "distributed" and dispatcher is not None:
            active_executor: TaskExecutor = RemoteTaskExecutor(dispatcher)
        else:
            active_executor = LocalTaskExecutor(client)
    else:
        active_executor = executor

    store = task_store or TaskStore(
        database_path=config.sqlite_database_path,
        results_dir=config.task_results_dir,
    )
    queue = task_queue or TaskQueue(
        task_store=store,
        agy_client=client,
        bot=None,
        executor=active_executor,
    )

    if dispatcher is not None and queue is not None:
        dispatcher.set_on_agent_online(queue.notify)

    # 1. Dependency injection into handlers
    dp["config"] = config
    dp["agy_client"] = client
    dp["agent_dispatcher"] = dispatcher
    dp["executor"] = active_executor
    dp["confirmation_store"] = confirmation_store or ConfirmationStore(
        default_ttl_seconds=float(config.confirmation_timeout_seconds)
    )
    dp["task_lock"] = task_lock if task_lock is not None else asyncio.Lock()
    dp["task_store"] = store
    dp["task_queue"] = queue

    # 2. Register security middlewares
    auth_middleware = AuthorizationMiddleware(config)
    dp.message.middleware(auth_middleware)
    dp.callback_query.middleware(auth_middleware)

    # 3. Register routes
    dp.include_router(main_router)

    # 4. Centralized error handling
    @dp.error()
    async def global_error_handler(event: ErrorEvent) -> Any:
        exception = event.exception
        if isinstance(exception, TelegramAPIError):
            logger.error(
                "Telegram API Error occurred: %s (Update ID: %s)",
                exception.message,
                event.update.update_id if event.update else "N/A",
            )
        else:
            logger.exception(
                "Unhandled exception in handler: %s (Update ID: %s)",
                type(exception).__name__,
                event.update.update_id if event.update else "N/A",
            )
        return True

    return dp
