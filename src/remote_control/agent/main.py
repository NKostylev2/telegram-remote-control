"""Console script entry point for running Windows Agent."""

from __future__ import annotations

import asyncio
import logging
import sys

from remote_control.agent.runner import AgentRunner
from remote_control.config import load_config

logger = logging.getLogger("remote_control.agent")


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(run_agent())
    except SystemExit:
        raise
    except Exception as e:
        logger.critical("Fatal error starting Windows Agent: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
