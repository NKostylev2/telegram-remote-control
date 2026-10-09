"""Pytest fixtures and configuration."""

import os
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear all REMOTE_CONTROL related environment variables before test."""
    prefixes = (
        "TELEGRAM_",
        "ALLOWED_",
        "ANTIGRAVITY_",
        "TASK_",
        "CONFIRMATION_",
        "TARGET_",
        "WOL_",
        "CONTROLLER_",
        "AGENT_",
    )
    for key in list(os.environ.keys()):
        if any(key.startswith(p) for p in prefixes):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def valid_config_dict(tmp_path: Path) -> dict[str, Any]:
    """Sample valid configuration dictionary for tests."""
    workspace_dir = tmp_path / "test_workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    return {
        "TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
        "ALLOWED_TELEGRAM_USERS": [111222333, 444555666],
        "ALLOWED_WORKSPACES": {"test_proj": str(workspace_dir)},
        "ANTIGRAVITY_CLI_PATH": "agy",
        "TASK_EXECUTION_TIMEOUT_SECONDS": 300,
        "TARGET_MAC_ADDRESS": "AA:BB:CC:DD:EE:FF",
        "WOL_BROADCAST_IP": "192.168.1.255",
        "WOL_PORT": 9,
        "CONTROLLER_MODE": "local",
        "CONTROLLER_HOST": "127.0.0.1",
        "CONTROLLER_PORT": 8080,
        "AGENT_AUTH_SECRET": "a_very_secure_random_secret_string_12345",
    }
