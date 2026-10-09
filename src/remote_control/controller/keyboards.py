"""Persistent reply and common keyboards for Telegram Remote Control."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Build persistent reply keyboard displayed at the bottom of the Telegram chat."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🚀 Запустить задачу"),
                KeyboardButton(text="📸 Снимок экрана"),
            ],
            [
                KeyboardButton(text="📊 Статус"),
                KeyboardButton(text="📋 Очередь задач"),
            ],
            [
                KeyboardButton(text="📁 Рабочая папка"),
                KeyboardButton(text="❓ Помощь"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
