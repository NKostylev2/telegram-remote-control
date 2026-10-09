"""Single-instance process lock to prevent concurrent workers on the same storage."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import TracebackType
from typing import IO

logger = logging.getLogger(__name__)


class SingleInstanceError(Exception):
    """Raised when another application instance is already running with the same lock."""


class SingleInstanceLock:
    """Cross-platform file-based lock enforcing a single active executor process."""

    def __init__(self, lock_file_path: Path) -> None:
        self.lock_file_path = lock_file_path
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        """Acquire exclusive non-blocking OS-level file lock.

        Raises:
            SingleInstanceError: If another instance already holds the lock.
        """
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = open(self.lock_file_path, "a+", encoding="utf-8")
        except OSError as e:
            raise SingleInstanceError(
                f"Cannot open lock file '{self.lock_file_path}': {e}"
            ) from e

        if sys.platform == "win32":
            import msvcrt

            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                self._cleanup_file()
                raise SingleInstanceError(
                    f"Другой экземпляр бота уже запущен и удерживает блокировку: "
                    f"'{self.lock_file_path}'"
                ) from e
        else:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as e:
                self._cleanup_file()
                raise SingleInstanceError(
                    f"Другой экземпляр бота уже запущен и удерживает блокировку: "
                    f"'{self.lock_file_path}'"
                ) from e

        logger.debug("Acquired single-instance lock on %s", self.lock_file_path)

    def release(self) -> None:
        """Release OS-level file lock and close descriptor."""
        if self._file is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                logger.debug("Failed unlocking file: %s", e)
            self._cleanup_file()
            logger.debug("Released single-instance lock on %s", self.lock_file_path)

    def _cleanup_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
