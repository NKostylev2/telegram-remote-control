"""Unit tests for filesystem browser utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from remote_control.controller.browser import (
    get_available_drives,
    get_parent_directory,
    is_drive_root,
    list_subdirectories,
)


def test_get_available_drives() -> None:
    """Test getting available drives."""
    drives = get_available_drives()
    assert isinstance(drives, list)
    assert len(drives) >= 1
    # On Windows, at least C:\ should be present
    assert any("C:\\" in d or "/" in d for d in drives)


def test_is_drive_root() -> None:
    """Test detecting drive root paths."""
    assert is_drive_root("C:\\") is True
    assert is_drive_root(Path("C:\\")) is True
    assert is_drive_root("C:\\Windows") is False
    assert is_drive_root(Path("C:\\Windows\\System32")) is False


def test_get_parent_directory() -> None:
    """Test getting parent directory and stopping at drive root."""
    assert get_parent_directory("C:\\") is None
    parent = get_parent_directory(Path("C:\\Windows\\System32"))
    assert parent is not None
    assert parent.name.lower() == "windows"


def test_list_subdirectories(tmp_path: Path) -> None:
    """Test safe listing and filtering of subdirectories."""
    # Create test folder structure
    (tmp_path / "normal_dir").mkdir()
    (tmp_path / "another_dir").mkdir()
    (tmp_path / ".hidden_dir").mkdir()
    (tmp_path / "$Recycle.Bin").mkdir()
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "regular_file.txt").write_text("hello", encoding="utf-8")

    subdirs = list_subdirectories(tmp_path)
    dir_names = [d.name for d in subdirs]

    assert "another_dir" in dir_names
    assert "normal_dir" in dir_names
    assert ".hidden_dir" not in dir_names
    assert "$Recycle.Bin" not in dir_names
    assert "System Volume Information" not in dir_names
    assert "regular_file.txt" not in dir_names
    # Sorting check: 'another_dir' before 'normal_dir'
    assert dir_names == ["another_dir", "normal_dir"]


def test_list_subdirectories_nonexistent_or_file(tmp_path: Path) -> None:
    """Test list_subdirectories with non-existent path or file."""
    non_existent = tmp_path / "does_not_exist"
    assert list_subdirectories(non_existent) == []

    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    assert list_subdirectories(file_path) == []


def test_list_subdirectories_permission_error(tmp_path: Path) -> None:
    """Test graceful handling when iterdir raises PermissionError."""
    with patch.object(Path, "iterdir", side_effect=PermissionError("Access denied")):
        assert list_subdirectories(tmp_path) == []
