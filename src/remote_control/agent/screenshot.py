"""Screen capture utility for Windows desktop with multi-session support."""

from __future__ import annotations

import ctypes
import io
import logging
import platform
from datetime import UTC, datetime
from typing import NamedTuple

logger = logging.getLogger(__name__)


class ScreenshotResult(NamedTuple):
    """Result of desktop screen capture."""

    image_bytes: bytes
    width: int
    height: int
    timestamp: datetime
    error: str | None = None


def capture_desktop_screenshot() -> ScreenshotResult:
    """Capture full desktop screenshot on Windows.

    Uses OpenInputDesktop to attach to active user desktop even if running in a background
    thread/service, then captures display via PIL ImageGrab.
    """
    now = datetime.now(UTC)

    if platform.system() != "Windows":
        return ScreenshotResult(
            image_bytes=b"",
            width=0,
            height=0,
            timestamp=now,
            error="Снимок экрана поддерживается только на ОС Windows.",
        )

    try:
        from PIL import ImageGrab
    except ImportError:
        return ScreenshotResult(
            image_bytes=b"",
            width=0,
            height=0,
            timestamp=now,
            error="Модуль Pillow не установлен.",
        )

    user32 = ctypes.windll.user32
    hdesk = None

    try:
        # 0x01FF covers DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP etc.
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)

        img = ImageGrab.grab()
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        raw_bytes = bio.getvalue()

        return ScreenshotResult(
            image_bytes=raw_bytes,
            width=img.width,
            height=img.height,
            timestamp=now,
            error=None,
        )
    except Exception as e:
        logger.warning("Desktop screen capture failed: %s", e)
        return ScreenshotResult(
            image_bytes=b"",
            width=0,
            height=0,
            timestamp=now,
            error=f"Ошибка захвата экрана: {e}. Возможно, рабочий стол заблокирован.",
        )
    finally:
        if hdesk:
            try:
                user32.CloseDesktop(hdesk)
            except Exception:
                pass
