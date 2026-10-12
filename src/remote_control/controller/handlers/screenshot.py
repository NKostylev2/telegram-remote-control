"""Screenshot handler for Telegram Remote Control."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from remote_control.agent.screenshot import capture_desktop_screenshot
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.keyboards import get_main_reply_keyboard

logger = logging.getLogger(__name__)

screenshot_router = Router(name="screenshot")


@screenshot_router.message(Command("screenshot"))
@screenshot_router.message(F.text.in_(["📸 Снимок экрана", "Снимок экрана"]))
async def cmd_screenshot(
    message: Message,
    config: AppConfig | None = None,
    agent_dispatcher: AgentDispatcher | None = None,
) -> None:
    is_distributed = (
        config is not None and getattr(config, "controller_mode", "local") == "distributed"
    )

    # In distributed mode, verify agent connection before proceeding
    if is_distributed:
        if agent_dispatcher is None or not agent_dispatcher.is_agent_connected():
            await message.answer(
                "❌ <b>Не удалось сделать снимок экрана</b>\n\n"
                "⚠️ <i>Windows Agent оффлайн. Включите компьютер через команду /wake.</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            return

    wait_msg = await message.answer(
        "📸 <i>Создаю снимок экрана рабочего стола...</i>",
        parse_mode="HTML",
    )

    if is_distributed and agent_dispatcher is not None:
        try:
            raw_result = await agent_dispatcher.request_screenshot(timeout_seconds=15.0)
        except Exception as e:
            await wait_msg.edit_text(
                "❌ <b>Не удалось сделать снимок экрана</b>\n\n"
                f"⚠️ <i>{html.escape(str(e))}</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            return

        if not raw_result:
            await wait_msg.edit_text(
                "❌ <b>Не удалось сделать снимок экрана</b>\n\n"
                "⚠️ <i>Windows Agent не ответил на запрос снимка экрана вовремя.</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            return

        now = datetime.now(UTC)
        ts_val = int(now.timestamp())
        ts_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")

        if isinstance(raw_result, bytes):
            photo_bytes = raw_result
            dim_str = "1920x1080 (Windows Desktop)"
        else:
            photo_bytes = getattr(raw_result, "image_bytes", b"")
            w = getattr(raw_result, "width", 1920)
            h = getattr(raw_result, "height", 1080)
            dim_str = f"{w}x{h}"
    else:
        result = await asyncio.to_thread(capture_desktop_screenshot)

        if result.error or not result.image_bytes:
            err_text = result.error or "Неизвестная ошибка захвата экрана."
            await wait_msg.edit_text(
                f"❌ <b>Не удалось сделать снимок экрана</b>\n\n"
                f"⚠️ <i>{html.escape(err_text)}</i>\n\n"
                "<i>(Проверьте, не заблокирован ли экран Windows комбинацией Win+L)</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            return

        ts_val = int(result.timestamp.timestamp())
        photo_bytes = result.image_bytes
        dim_str = f"{result.width}x{result.height}"
        ts_str = result.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    photo_file = BufferedInputFile(
        photo_bytes,
        filename=f"screenshot_{ts_val}.jpg",
    )
    caption = (
        "📸 <b>Снимок экрана рабочего стола</b>\n\n"
        f"🖥️ Разрешение: <code>{dim_str}</code>\n"
        f"⏱️ Время: <code>{ts_str}</code>"
    )

    send_successful = False

    # 1. Primary delivery: photo
    try:
        await message.answer_photo(
            photo=photo_file,
            caption=caption,
            parse_mode="HTML",
            reply_markup=get_main_reply_keyboard(),
        )
        send_successful = True
    except Exception as photo_err:
        logger.warning(
            "Failed sending screenshot as photo: %s. Falling back to document delivery.",
            photo_err,
        )
        # 2. Fallback delivery: document
        try:
            doc_bytes = result.original_bytes or result.image_bytes
            doc_file = BufferedInputFile(
                doc_bytes,
                filename=f"screenshot_{ts_val}.jpg",
            )
            await message.answer_document(
                document=doc_file,
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            send_successful = True
        except Exception as doc_err:
            logger.error("Failed sending screenshot as document fallback: %s", doc_err)
            await wait_msg.edit_text(
                "❌ <b>Не удалось отправить снимок экрана</b>\n\n"
                "⚠️ <i>Ошибка передачи данных в Telegram. Повторите попытку позже.</i>",
                parse_mode="HTML",
                reply_markup=get_main_reply_keyboard(),
            )
            return

    # Delete waiting message ONLY after successful delivery
    if send_successful:
        try:
            await wait_msg.delete()
        except Exception as e:
            logger.debug("Could not delete waiting message: %s", e)
