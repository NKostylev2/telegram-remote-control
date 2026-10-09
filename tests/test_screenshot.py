"""Tests for desktop screenshot capture and Telegram /screenshot handler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Chat, Message, User

from remote_control.agent.screenshot import ScreenshotResult, capture_desktop_screenshot
from remote_control.controller.handlers.screenshot import cmd_screenshot


def test_capture_desktop_screenshot_mocked_success() -> None:
    """Test successful desktop screen capture with mocked Pillow ImageGrab."""
    mock_img = MagicMock()
    mock_img.width = 1920
    mock_img.height = 1080
    mock_img.save.side_effect = lambda bio, format: bio.write(b"\x89PNG\r\n\x1a\nfakeimagebytes")

    with patch("platform.system", return_value="Windows"):
        with patch("ctypes.windll.user32.OpenInputDesktop", return_value=123):
            with patch("ctypes.windll.user32.SetThreadDesktop", return_value=1):
                with patch("ctypes.windll.user32.CloseDesktop", return_value=1):
                    with patch("PIL.ImageGrab.grab", return_value=mock_img):
                        res = capture_desktop_screenshot()
                        assert res.error is None
                        assert res.width == 1920
                        assert res.height == 1080
                        assert len(res.image_bytes) > 0


def test_capture_desktop_screenshot_non_windows() -> None:
    """Test capture returns error on non-Windows OS."""
    with patch("platform.system", return_value="Linux"):
        res = capture_desktop_screenshot()
        assert res.error is not None
        assert "Windows" in res.error
        assert len(res.image_bytes) == 0


def test_capture_desktop_screenshot_locked_screen() -> None:
    """Test capture handles locked desktop gracefully."""
    with patch("platform.system", return_value="Windows"):
        with patch("ctypes.windll.user32.OpenInputDesktop", side_effect=OSError("Access denied")):
            res = capture_desktop_screenshot()
            assert res.error is not None
            assert len(res.image_bytes) == 0


def test_cmd_screenshot_handler_success() -> None:
    """Test /screenshot handler sends photo when capture succeeds."""
    mock_user = User(id=111222333, is_bot=False, first_name="Kostya")
    mock_chat = Chat(id=111222333, type="private")
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = mock_user
    mock_msg.chat = mock_chat

    wait_msg = MagicMock()
    wait_msg.delete = AsyncMock()
    mock_msg.answer = AsyncMock(return_value=wait_msg)
    mock_msg.answer_photo = AsyncMock()

    mock_result = ScreenshotResult(
        image_bytes=b"\x89PNG\r\n\x1a\nfakebytes",
        width=2560,
        height=1440,
        timestamp=MagicMock(
            timestamp=lambda: 123456789.0,
            strftime=lambda fmt: "2026-10-09 12:00:00 UTC",
        ),
        error=None,
    )

    with patch(
        "remote_control.controller.handlers.screenshot.capture_desktop_screenshot",
        return_value=mock_result,
    ):
        asyncio.run(cmd_screenshot(mock_msg))

        mock_msg.answer.assert_awaited_once()
        wait_msg.delete.assert_awaited_once()
        mock_msg.answer_photo.assert_awaited_once()
        _, kwargs = mock_msg.answer_photo.call_args
        assert "2560x1440" in kwargs["caption"]
        assert kwargs["reply_markup"] is not None


def test_cmd_screenshot_handler_failure() -> None:
    """Test /screenshot handler shows error message when capture fails."""
    mock_user = User(id=111222333, is_bot=False, first_name="Kostya")
    mock_chat = Chat(id=111222333, type="private")
    mock_msg = MagicMock(spec=Message)
    mock_msg.from_user = mock_user
    mock_msg.chat = mock_chat

    wait_msg = MagicMock()
    wait_msg.edit_text = AsyncMock()
    mock_msg.answer = AsyncMock(return_value=wait_msg)

    mock_result = ScreenshotResult(
        image_bytes=b"",
        width=0,
        height=0,
        timestamp=MagicMock(),
        error="Рабочий стол заблокирован.",
    )

    with patch(
        "remote_control.controller.handlers.screenshot.capture_desktop_screenshot",
        return_value=mock_result,
    ):
        asyncio.run(cmd_screenshot(mock_msg))

        mock_msg.answer.assert_awaited_once()
        wait_msg.edit_text.assert_awaited_once()
        text = wait_msg.edit_text.call_args[0][0]
        assert "Не удалось сделать снимок" in text
        assert "Рабочий стол заблокирован" in text
