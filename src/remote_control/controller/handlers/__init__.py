"""Telegram command handlers package."""

from aiogram import Router

from remote_control.controller.handlers.base import base_router
from remote_control.controller.handlers.files import files_router
from remote_control.controller.handlers.run import run_router
from remote_control.controller.handlers.screenshot import screenshot_router
from remote_control.controller.handlers.tasks import tasks_router

main_router = Router(name="main_router")
main_router.include_router(base_router)
main_router.include_router(screenshot_router)
main_router.include_router(files_router)
main_router.include_router(run_router)
main_router.include_router(tasks_router)

__all__ = [
    "main_router",
    "base_router",
    "files_router",
    "screenshot_router",
    "run_router",
    "tasks_router",
]

