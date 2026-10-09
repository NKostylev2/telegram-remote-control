"""Windows Agent package for Telegram Remote Control.

The Windows Agent runs as a background process under the user session on the target Windows PC,
executes Antigravity CLI subprocesses in allowed workspaces, and reports results.
"""

from remote_control.agent.agy_client import (
    AgyClient,
    AgyClientError,
    CliNotFoundError,
    ExecutionCancelledError,
    ExecutionTimeoutError,
    JsonParseError,
    WorkspaceDeniedError,
    WorkspaceInvalidError,
    WorkspaceNotFoundError,
)

__all__ = [
    "AgyClient",
    "AgyClientError",
    "CliNotFoundError",
    "ExecutionCancelledError",
    "ExecutionTimeoutError",
    "JsonParseError",
    "WorkspaceDeniedError",
    "WorkspaceInvalidError",
    "WorkspaceNotFoundError",
]
