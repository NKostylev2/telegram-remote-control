"""Screenshot handler for Telegram Remote Control."""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from remote_control.agent.screenshot import capture_desktop_screenshot
from remote_control.controller.keyboards import get_main_reply_keyboard

logger = logging.getLogger(__name__)

screenshot_router = Router(name="screenshot")


@screenshot_router.message(Command("screenshot"))
@screenshot_router.message(F.text.in_(["📸 Снимок экрана", "Снимок экрана"]))
async def cmd_screenshot(message: Message) -> None:
    """Capture current desktop screen and send it as a photo to Telegram."""
    wait_msg = await message.answer(
        "📸 <i>Создаю снимок экрана рабочего стола...</i>",
        parse_mode="HTML",
    )

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

    photo_file = BufferedInputFile(
        result.image_bytes,
        filename=f"screenshot_{int(result.timestamp.timestamp())}.png",
    )
    caption = (
        "📸 <b>Снимок экрана рабочего стола</b>\n\n"
        f"🖥️ Разрешение: <code>{result.width}x{result.height}</code>\n"
        f"⏱️ Время: <code>{result.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>"
    )

    try:
        await wait_msg.delete()
    except Exception as e:
        logger.debug("Could not delete waiting message: %s", e)

    await message.answer_photo(
        photo=photo_file,
        caption=caption,
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard(),
    )
