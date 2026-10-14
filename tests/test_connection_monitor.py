"""Unit tests for ConnectionMonitor: throttled, visible agent connectivity logging."""

from __future__ import annotations

import logging

import aiohttp
import pytest

from remote_control.agent.connection_monitor import ConnectionMonitor, describe_error

LOGGER_NAME = "remote_control.agent.connection"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _monitor(clock: FakeClock) -> ConnectionMonitor:
    return ConnectionMonitor("heartbeat", "http://192.168.0.20:8080", 60.0, clock=clock)


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == level]


def test_describe_error_handles_empty_message() -> None:
    assert describe_error(TimeoutError()) == "TimeoutError"
    assert describe_error(OSError("boom")) == "OSError: boom"


def test_first_failure_is_logged_immediately_as_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    monitor = _monitor(FakeClock())

    monitor.failure(OSError("connection refused"))

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "192.168.0.20:8080" in text
    assert "connection refused" in text
    assert "CONTROLLER_HOST" in text  # actionable hint
    assert not monitor.is_connected


def test_repeated_failures_are_throttled_then_logged_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    clock = FakeClock()
    monitor = _monitor(clock)

    for _ in range(10):
        monitor.failure(OSError("down"))
        clock.advance(5.0)  # 50 s total: still inside the 60 s window
    assert len(_records(caplog, logging.WARNING)) == 1

    clock.advance(20.0)  # past the repeat interval
    monitor.failure(OSError("down"))
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert "11" in warnings[1].getMessage()  # consecutive failure counter is reported


def test_first_success_logs_connected_and_is_not_repeated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monitor = _monitor(FakeClock())

    monitor.success()
    monitor.success()
    monitor.success()

    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert "Подключено" in infos[0].getMessage()
    assert monitor.is_connected


def test_success_after_failures_logs_restored(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    monitor = _monitor(FakeClock())

    monitor.failure(OSError("down"))
    monitor.failure(OSError("down"))
    monitor.success()

    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert "восстановлена" in infos[0].getMessage()
    assert monitor.consecutive_failures == 0


def test_http_error_is_reported_with_status(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    monitor = _monitor(FakeClock())
    error = aiohttp.ClientResponseError(
        request_info=None,  # type: ignore[arg-type]
        history=(),
        status=500,
        message="Internal Server Error",
    )

    monitor.failure(error)

    text = _records(caplog, logging.WARNING)[0].getMessage()
    assert "500" in text
    assert "Internal Server Error" in text


def test_unexpected_exception_is_logged_as_error_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    monitor = _monitor(FakeClock())

    monitor.failure(ValueError("bad payload"))

    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "bad payload" in errors[0].getMessage()


def test_unauthorized_is_logged_as_error_and_throttled(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    clock = FakeClock()
    monitor = _monitor(clock)

    for _ in range(5):
        monitor.unauthorized(PermissionError("401"))
        clock.advance(5.0)

    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "AGENT_AUTH_SECRET" in errors[0].getMessage()
    assert not monitor.is_connected
