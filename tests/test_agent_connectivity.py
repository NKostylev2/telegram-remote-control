"""Acceptance tests: real ControllerServer + real AgentRunner over localhost HTTP.

These reproduce the situations that previously failed silently on the real devices:
the agent starts but never reaches the controller. Each test asserts both the
*behaviour* (controller sees the agent) and the *visibility* (the agent logs why not).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

from remote_control.agent.agy_client import AgyClient
from remote_control.agent.runner import AgentRunner
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.server import ControllerServer

SECRET = "acceptance_test_shared_secret_123"
OTHER_SECRET = "a_completely_different_secret_456"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(port: int, secret: str = SECRET) -> AppConfig:
    return AppConfig(
        CONTROLLER_MODE="distributed",
        CONTROLLER_HOST="127.0.0.1",
        CONTROLLER_PORT=port,
        AGENT_AUTH_SECRET=secret,
    )


def _runner(config: AppConfig) -> AgentRunner:
    agy = MagicMock(spec=AgyClient)
    agy.is_cli_available.return_value = True
    runner = AgentRunner(config=config, agy_client=agy, agent_id="acceptance-agent")
    runner.HEARTBEAT_INTERVAL_SECONDS = 0.1
    return runner


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _shutdown(runner: AgentRunner, task: asyncio.Task[None]) -> None:
    await runner.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_agent_connects_and_controller_reports_it_online(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    async def _test() -> None:
        port = _free_port()
        config = _config(port)
        dispatcher = AgentDispatcher(config)
        server = ControllerServer(config, dispatcher)
        await server.start()
        runner = _runner(config)
        task = asyncio.create_task(runner.start())
        try:
            assert await _wait_for(dispatcher.is_agent_connected), "agent never connected"
            assert await _wait_for(lambda: runner._poll_monitor.is_connected)

            # The same check a human (or a deploy script) would run: GET /api/v1/status
            async with aiohttp.ClientSession() as http:
                async with http.get(f"http://127.0.0.1:{port}/api/v1/status") as resp:
                    status = await resp.json()
            assert status["agent_connected"] is True
            assert status["agent_ready"] is True
        finally:
            await _shutdown(runner, task)
            await server.stop()

    asyncio.run(_test())
    assert any("Подключено к Controller" in m for m in _messages(caplog, logging.INFO))


def test_unreachable_controller_is_visible_in_agent_log(caplog: pytest.LogCaptureFixture) -> None:
    """Regression: connection errors used to be logged at DEBUG, i.e. never shown."""
    caplog.set_level(logging.INFO)  # same level the real entry point uses

    async def _test() -> None:
        port = _free_port()  # nothing is listening here
        runner = _runner(_config(port))
        task = asyncio.create_task(runner.start())
        try:
            assert await _wait_for(
                lambda: any(
                    f"127.0.0.1:{port}" in m for m in _messages(caplog, logging.WARNING)
                ),
                timeout=3.0,
            ), "agent stayed silent although the controller is unreachable"
        finally:
            await _shutdown(runner, task)

    asyncio.run(_test())
    warning = next(m for m in _messages(caplog, logging.WARNING) if "Нет связи" in m)
    assert "CONTROLLER_HOST" in warning


def test_wrong_secret_is_rejected_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    async def _test() -> None:
        port = _free_port()
        controller_cfg = _config(port, SECRET)
        dispatcher = AgentDispatcher(controller_cfg)
        server = ControllerServer(controller_cfg, dispatcher)
        await server.start()
        runner = _runner(_config(port, OTHER_SECRET))
        task = asyncio.create_task(runner.start())
        try:
            assert await _wait_for(
                lambda: any("AGENT_AUTH_SECRET" in m for m in _messages(caplog, logging.ERROR)),
                timeout=3.0,
            ), "agent did not report the authorization failure"
            assert not dispatcher.is_agent_connected()
        finally:
            await _shutdown(runner, task)
            await server.stop()

    asyncio.run(_test())


def test_agent_started_before_controller_connects_when_it_appears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PC boots faster than the phone's controller: the agent must keep retrying."""
    caplog.set_level(logging.INFO)

    async def _test() -> None:
        port = _free_port()
        config = _config(port)
        dispatcher = AgentDispatcher(config)
        server = ControllerServer(config, dispatcher)
        runner = _runner(config)
        task = asyncio.create_task(runner.start())
        try:
            assert await _wait_for(
                lambda: any("Нет связи" in m for m in _messages(caplog, logging.WARNING)),
                timeout=3.0,
            )
            assert not dispatcher.is_agent_connected()

            await server.start()  # the controller comes up later
            assert await _wait_for(dispatcher.is_agent_connected, timeout=5.0), (
                "agent did not reconnect after the controller became available"
            )
        finally:
            await _shutdown(runner, task)
            await server.stop()

    asyncio.run(_test())
    assert any("восстановлена" in m for m in _messages(caplog, logging.INFO))


def test_controller_exits_with_clear_error_when_port_is_taken(
    clean_env: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale controller holding the port must give a readable error and a clean exit."""
    from remote_control.controller.main import run_bot
    from remote_control.controller.single_instance import SingleInstanceLock

    caplog.set_level(logging.INFO)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = int(blocker.getsockname()[1])
    config = AppConfig(
        TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
        ALLOWED_TELEGRAM_USERS=[111],
        CONTROLLER_MODE="distributed",
        CONTROLLER_HOST="127.0.0.1",
        CONTROLLER_PORT=port,
        AGENT_AUTH_SECRET=SECRET,
        SQLITE_DATABASE_PATH=tmp_path / "tasks.sqlite3",
        TASK_RESULTS_DIR=tmp_path / "results",
    )
    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(run_bot(config))
    finally:
        blocker.close()

    assert exc.value.code == 1
    assert any("Не удалось запустить HTTP API" in m for m in _messages(caplog, logging.ERROR))
    # The single-instance lock must have been released, otherwise a restart loop would deadlock.
    lock = SingleInstanceLock(config.sqlite_database_path.with_suffix(".lock"))
    lock.acquire()
    lock.release()
