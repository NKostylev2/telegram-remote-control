"""Persistent reply and common keyboards for Telegram Remote Control."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


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
    )


def build_task_cancel_keyboard(task_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard with a Cancel button for a running or queued task."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить задачу",
                    callback_data=f"task:cancel:{task_id}",
                )
            ]
        ]
    )
