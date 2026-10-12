"""Filesystem directory browser utilities for interactive Telegram navigation."""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path

# System folder names to exclude from browsing for security and cleanliness
_EXCLUDED_FOLDER_NAMES = {
    "System Volume Information",
    "$Recycle.Bin",
    "Recovery",
    "PerfLogs",
    "Config.Msi",
}


def get_available_drives() -> list[str]:
    """Return available system drives on Windows, or root directory on POSIX systems."""
    if sys.platform == "win32":
        try:
            import ctypes

            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            if bitmask:
                drives: list[str] = []
                for i, letter in enumerate(string.ascii_uppercase):
                    if bitmask & (1 << i):
                        drives.append(f"{letter}:\\")
                if drives:
                    return drives
        except Exception:
            pass

        # Fallback if Windows API is unavailable
        drives = []
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            try:
                if os.path.exists(drive_path):
                    drives.append(drive_path)
            except OSError:
                continue
        return drives or ["C:\\"]
    return ["/"]


def is_drive_root(path: Path | str) -> bool:
    """Check whether the given path represents a drive or filesystem root."""
    p = Path(path).resolve()
    return p == Path(p.anchor).resolve() or p.parent == p


def get_parent_directory(path: Path | str) -> Path | None:
    """Return the parent directory, or None if already at drive/system root."""
    p = Path(path).resolve()
    if is_drive_root(p):
        return None
    return p.parent


def list_subdirectories(path: Path | str) -> list[Path]:
    """Safely return sorted accessible subdirectories of a given folder using os.scandir().

    Excludes hidden files, system folders, and handles permission errors gracefully.
    """
    p = Path(path).resolve()
    try:
        if not p.is_dir():
            return []
    except (PermissionError, OSError):
        return []

    subdirs: list[Path] = []
    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith((".", "$"))
                        and entry.name not in _EXCLUDED_FOLDER_NAMES
                    ):
                        subdirs.append(Path(entry.path))
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        return []

    return sorted(subdirs, key=lambda item: item.name.lower())
