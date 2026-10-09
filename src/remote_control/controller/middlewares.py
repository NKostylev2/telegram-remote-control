"""Authorization and security middleware for Telegram Remote Control bot."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from remote_control.config import AppConfig

logger = logging.getLogger(__name__)


class AuthorizationMiddleware(BaseMiddleware):
    """Enforces strict allowlisting, chat type restrictions, and stealth mode."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Determine sender user and chat
        user_id: int | None = None
        chat_type: str | None = None
        event_type = type(event).__name__

        if isinstance(event, Message):
            if event.from_user:
                user_id = event.from_user.id
            if event.chat:
                chat_type = event.chat.type
        elif isinstance(event, CallbackQuery):
            if event.from_user:
                user_id = event.from_user.id
            if event.message and event.message.chat:
                chat_type = event.message.chat.type

        # 1. Require private chat
        if chat_type is not None and chat_type != ChatType.PRIVATE:
            logger.warning(
                "Ignored non-private event: user_id=%s, chat_type=%s, event_type=%s",
                user_id,
                chat_type,
                event_type,
            )
            return None

        # 2. Check allowlist configuration
        allowed_users = self.config.allowed_telegram_users
        if not allowed_users:
            logger.warning(
                "Access denied: ALLOWED_TELEGRAM_USERS is empty. Request dropped (user_id=%s).",
                user_id,
            )
            return None

        # 3. Check sender user ID
        if user_id is None or user_id not in allowed_users:
            # Stealth mode: drop silently without answering
            logger.warning(
                "Stealth-drop: Unauthorized user_id=%s attempted %s.",
                user_id,
                event_type,
            )
            return None

        # Authorized user
        logger.info(
            "Authorized event: user_id=%s, event_type=%s",
            user_id,
            event_type,
        )
        return await handler(event, data)
