"""Throttled, human-visible connectivity logging for the Windows Agent.

The agent talks to the Controller from several background loops. Connection
errors in those loops used to be logged at DEBUG level, so an agent that was
running but could not reach the Controller produced no output at all: the
process looked healthy while nothing worked.

``ConnectionMonitor`` turns the raw stream of successes and failures into a few
meaningful lines instead of a flood:

- first failure                  -> WARNING with an actionable hint
- repeated failures              -> WARNING at most once per ``repeat_interval``
- first success                  -> INFO "connected"
- success after failures         -> INFO "restored"
- HTTP 401 from the Controller   -> ERROR (throttled the same way)
- non-network exception          -> ERROR with traceback (throttled)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import aiohttp

_NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

_STATE_UNKNOWN = "unknown"
_STATE_UP = "up"
_STATE_DOWN = "down"


def describe_error(error: BaseException) -> str:
    """Return a readable one-line description (some exceptions have an empty str())."""
    text = str(error).strip()
    name = type(error).__name__
    return f"{name}: {text}" if text else name


class ConnectionMonitor:
    """Track reachability of one agent -> Controller channel and log state changes."""

    def __init__(
        self,
        channel: str,
        target: str,
        repeat_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        log: logging.Logger | None = None,
    ) -> None:
        self.channel = channel
        self.target = target
        self.repeat_interval = repeat_interval
        self._clock = clock
        self._log = log or logging.getLogger("remote_control.agent.connection")

        self._state = _STATE_UNKNOWN
        self._consecutive_failures = 0
        self._last_failure_log: float | None = None
        self._last_auth_log: float | None = None

    @property
    def is_connected(self) -> bool:
        """True when the most recent request on this channel succeeded."""
        return self._state == _STATE_UP

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _throttled(self, last: float | None) -> tuple[bool, float | None]:
        """Return (should_log, new_last_timestamp)."""
        now = self._clock()
        if last is None or now - last >= self.repeat_interval:
            return True, now
        return False, last

    def success(self) -> None:
        """Record a successful request."""
        if self._state == _STATE_UP:
            return
        if self._consecutive_failures:
            self._log.info(
                "[%s] Связь с Controller %s восстановлена (неудачных попыток до этого: %d).",
                self.channel,
                self.target,
                self._consecutive_failures,
            )
        else:
            self._log.info("[%s] Подключено к Controller %s.", self.channel, self.target)
        self._state = _STATE_UP
        self._consecutive_failures = 0
        self._last_failure_log = None
        self._last_auth_log = None

    def failure(self, error: BaseException) -> None:
        """Record a failed request (network error, HTTP error or unexpected exception)."""
        self._state = _STATE_DOWN
        self._consecutive_failures += 1
        should_log, self._last_failure_log = self._throttled(self._last_failure_log)
        if not should_log:
            return

        if isinstance(error, aiohttp.ClientResponseError):
            self._log.warning(
                "[%s] Controller %s ответил ошибкой HTTP %s (%s). Подряд неудач: %d.",
                self.channel,
                self.target,
                error.status,
                error.message,
                self._consecutive_failures,
            )
        elif isinstance(error, _NETWORK_ERRORS):
            self._log.warning(
                "[%s] Нет связи с Controller %s: %s. Подряд неудач: %d. "
                "Проверьте CONTROLLER_HOST/CONTROLLER_PORT в .env на ПК (должен быть реальный "
                "IP устройства с контроллером, не 0.0.0.0 и не 127.0.0.1), что контроллер "
                "запущен, а на его стороне CONTROLLER_HOST=0.0.0.0.",
                self.channel,
                self.target,
                describe_error(error),
                self._consecutive_failures,
            )
        else:
            self._log.error(
                "[%s] Неожиданная ошибка при работе с Controller %s: %s. Подряд неудач: %d.",
                self.channel,
                self.target,
                describe_error(error),
                self._consecutive_failures,
                exc_info=error,
            )

    def unauthorized(self, error: BaseException) -> None:
        """Record an authentication rejection (HTTP 401) from the Controller."""
        self._state = _STATE_DOWN
        self._consecutive_failures += 1
        should_log, self._last_auth_log = self._throttled(self._last_auth_log)
        if should_log:
            self._log.error(
                "[%s] Controller %s отклонил авторизацию: %s. Проверьте, что AGENT_AUTH_SECRET "
                "одинаков на ПК и на контроллере и что часы устройств расходятся не более чем "
                "на 60 секунд.",
                self.channel,
                self.target,
                describe_error(error),
            )
