"""Controller package for Telegram Remote Control.

Provides the Telegram Bot interface, authorization middlewares, and CLI dispatching.
"""

from remote_control.controller.bot import create_bot, create_dispatcher
from remote_control.controller.confirmation import (
    ConfirmationStatus,
    ConfirmationStore,
    PendingConfirmation,
)
from remote_control.controller.main import main, run_bot

__all__ = [
    "ConfirmationStatus",
    "ConfirmationStore",
    "PendingConfirmation",
    "create_bot",
    "create_dispatcher",
    "main",
    "run_bot",
]
