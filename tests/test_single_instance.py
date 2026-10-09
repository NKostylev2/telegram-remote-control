"""Tests for SingleInstanceLock preventing duplicate active processes."""

from pathlib import Path

import pytest

from remote_control.controller.single_instance import (
    SingleInstanceError,
    SingleInstanceLock,
)


def test_single_instance_lock_success(tmp_path: Path) -> None:
    """Can acquire and release single instance lock cleanly."""
    lock_file = tmp_path / "test.lock"
    lock = SingleInstanceLock(lock_file)
    lock.acquire()
    assert lock._file is not None

    lock.release()
    assert lock._file is None


def test_second_instance_rejected_with_error(tmp_path: Path) -> None:
    """Req 22: Second instance cannot acquire lock on the same file."""
    lock_file = tmp_path / "app.lock"
    lock1 = SingleInstanceLock(lock_file)
    lock1.acquire()

    lock2 = SingleInstanceLock(lock_file)
    with pytest.raises(SingleInstanceError, match="удерживает блокировку"):
        lock2.acquire()

    # Release lock1; now lock2 can acquire it
    lock1.release()
    lock2.acquire()
    assert lock2._file is not None
    lock2.release()


def test_single_instance_context_manager(tmp_path: Path) -> None:
    """Context manager correctly acquires and releases lock."""
    lock_file = tmp_path / "ctx.lock"
    with SingleInstanceLock(lock_file) as lock:
        assert lock._file is not None
        # Second instance within context should fail
        with pytest.raises(SingleInstanceError):
            SingleInstanceLock(lock_file).acquire()

    # Outside context, another instance can acquire
    with SingleInstanceLock(lock_file) as lock2:
        assert lock2._file is not None
