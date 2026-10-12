"""Screen capture utility for Windows desktop with multi-session support."""

from __future__ import annotations

import ctypes
import io
import logging
import platform
from datetime import UTC, datetime
from typing import NamedTuple

logger = logging.getLogger(__name__)

_MAX_DIMENSION = 2560
_JPEG_QUALITY = 85


class ScreenshotResult(NamedTuple):
    """Result of desktop screen capture."""

    image_bytes: bytes
    width: int
    height: int
    timestamp: datetime
    error: str | None = None
    original_bytes: bytes | None = None


def capture_desktop_screenshot() -> ScreenshotResult:
    """Capture full desktop screenshot on Windows with JPEG compression and auto-downscaling.

    Uses OpenInputDesktop to attach to active user desktop even if running in a background
    thread/service, then captures display via PIL ImageGrab (all_screens=True for virtual desktop).
    Downscales images exceeding 2560px on longest dimension using LANCZOS to preserve readability.
    Compresses to JPEG (quality ~85) to stay within Telegram payload and network limits.
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
        from PIL import Image, ImageGrab
    except ImportError:
        return ScreenshotResult(
            image_bytes=b"",
            width=0,
            height=0,
            timestamp=now,
            error="Модуль Pillow не установлен.",
        )

    windll = getattr(ctypes, "windll", None)
    user32 = getattr(windll, "user32", None) if windll else None
    hdesk = None

    try:
        # 0x01FF covers DESKTOP_READOBJECTS | DESKTOP_WRITEOBJECTS | DESKTOP_SWITCHDESKTOP etc.
        if user32:
            hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)

        # Capture virtual desktop spanning all monitors if available
        try:
            img = ImageGrab.grab(all_screens=True)
        except TypeError:
            img = ImageGrab.grab()

        orig_w = img.width
        orig_h = img.height

        # If RGBA, convert to RGB for JPEG
        if getattr(img, "mode", "RGB") != "RGB":
            rgb_img = img.convert("RGB")
        else:
            rgb_img = img

        orig_bio = io.BytesIO()
        rgb_img.save(orig_bio, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        original_bytes = orig_bio.getvalue()

        # Downscale if exceeding 2560px on longest side
        max_dim = max(orig_w, orig_h)
        if max_dim > _MAX_DIMENSION:
            scale = _MAX_DIMENSION / max_dim
            new_w = max(1, int(orig_w * scale))
            new_h = max(1, int(orig_h * scale))
            resample_filter = getattr(Image, "Resampling", Image).LANCZOS
            scaled_img = rgb_img.resize((new_w, new_h), resample=resample_filter)
            final_w, final_h = new_w, new_h
        else:
            scaled_img = rgb_img
            final_w, final_h = orig_w, orig_h

        bio = io.BytesIO()
        scaled_img.save(bio, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        final_bytes = bio.getvalue()

        return ScreenshotResult(
            image_bytes=final_bytes,
            width=final_w,
            height=final_h,
            timestamp=now,
            error=None,
            original_bytes=original_bytes,
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
        if hdesk and user32:
            try:
                user32.CloseDesktop(hdesk)
            except Exception:
                pass
