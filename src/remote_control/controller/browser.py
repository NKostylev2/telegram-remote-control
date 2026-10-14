"""Filesystem directory browser utilities for interactive Telegram navigation."""

from __future__ import annotations

import ntpath
import os
import string
import sys
from pathlib import Path

from remote_control.common.protocol import FileItem

# System folder names to exclude from browsing for security and cleanliness
_EXCLUDED_FOLDER_NAMES = {
    "System Volume Information",
    "$Recycle.Bin",
    "Recovery",
    "PerfLogs",
    "Config.Msi",
}


def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable size string (e.g. 1.2 MB, 450 KB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"



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
    path_str = str(path)
    if ":" in path_str or "\\" in path_str:
        drive, rest = ntpath.splitdrive(path_str)
        if drive and rest in ("\\", "/", "", "\\\\"):
            return True
    p = Path(path)
    return p == Path(p.anchor) or p.parent == p


def get_parent_directory(path: Path | str) -> Path | None:
    """Return the parent directory, or None if already at drive/system root."""
    if is_drive_root(path):
        return None
    path_str = str(path)
    if ":" in path_str or "\\" in path_str:
        parent_str = ntpath.dirname(path_str.rstrip("\\/"))
        if parent_str and not parent_str.endswith("\\") and ":" in parent_str:
            parent_str += "\\"
        return Path(parent_str) if parent_str else None
    p = Path(path)
    return p.parent


def list_directory_contents(path: Path | str) -> tuple[list[Path], list[FileItem]]:
    """Safely return sorted accessible subdirectories and files in a folder using os.scandir().

    Excludes hidden entries and system folders, handles permission errors gracefully.
    """
    p = Path(path).resolve()
    try:
        if not p.is_dir():
            return [], []
    except (PermissionError, OSError):
        return [], []

    subdirs: list[Path] = []
    files: list[FileItem] = []

    try:
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.name.startswith((".", "$")) or entry.name in _EXCLUDED_FOLDER_NAMES:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        stat = entry.stat(follow_symlinks=False)
                        files.append(
                            FileItem(
                                name=entry.name,
                                size_bytes=stat.st_size,
                                modified_at=stat.st_mtime,
                            )
                        )
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        return [], []

    subdirs_sorted = sorted(subdirs, key=lambda item: item.name.lower())
    files_sorted = sorted(files, key=lambda item: item.name.lower())
    return subdirs_sorted, files_sorted


def list_subdirectories(path: Path | str) -> list[Path]:
    """Safely return sorted accessible subdirectories of a given folder using os.scandir().

    Excludes hidden files, system folders, and handles permission errors gracefully.
    """
    subdirs, _ = list_directory_contents(path)
    return subdirs

