"""Console script entry point for running Windows Agent."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from remote_control.agent.runner import AgentRunner
from remote_control.config import load_config

logger = logging.getLogger("remote_control.agent")

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


def default_log_path() -> Path:
    """Return the agent log file location (%LOCALAPPDATA%\\RemoteControl\\logs on Windows)."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = (
        Path(local_app_data) / "RemoteControl"
        if local_app_data
        else Path.home() / ".remote-control"
    )
    return root / "logs" / "agent.log"


def configure_logging(log_path: Path | None = None) -> Path | None:
    """Log to the console and to a rotating file.

    The agent is usually started minimized from the Windows Startup folder, so a console
    alone leaves no trace once the window is closed or the process dies. The file makes
    "why does nothing work after boot" answerable. Returns the log path, or None if the
    file could not be opened (console logging still works).
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path: Path | None = log_path or default_log_path()
    file_error: OSError | None = None
    try:
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    except OSError as e:
        file_error = e
        path = None

    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATE_FORMAT,
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logger.warning("Не удалось открыть файл лога агента: %s", file_error)
    else:
        logger.info("Лог агента: %s", path)
    return path


async def run_agent() -> None:
    """Initialize configuration and launch the Windows Agent loop."""
    try:
        config = load_config()
    except Exception as e:
        logger.error("Configuration load error: %s", e)
        sys.exit(1)

    if not config.agent_auth_secret:
        logger.warning(
            "AGENT_AUTH_SECRET is not configured in .env. "
            "Agent will connect without cryptographic authentication."
        )

    runner = AgentRunner(config=config)

    try:
        await runner.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Termination signal received.")
    except Exception as e:
        logger.exception("Unexpected error in Windows Agent: %s", e)
        sys.exit(1)
    finally:
        await runner.stop()


def main() -> None:
    """Main CLI entry point for console script remote-control-agent."""
    configure_logging()
    try:
        asyncio.run(run_agent())
    except SystemExit:
        raise
    except Exception as e:
        logger.critical("Fatal error starting Windows Agent: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
