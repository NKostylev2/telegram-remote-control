"""Configuration management for Telegram Remote Control.

Provides type-safe configuration loading, environment parsing, and strict validation.
Ensures secrets are never exposed in exceptions, string representations, or logs.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    SecretStr,
    field_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Regex patterns
_MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")
_TELEGRAM_TOKEN_REGEX = re.compile(r"^\d{6,14}:[A-Za-z0-9_-]{20,}$")
_ALIAS_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")


class AppConfig(BaseSettings):
    """Main application configuration loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 1. Telegram settings
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        alias="TELEGRAM_BOT_TOKEN",
        description="Telegram bot token obtained from @BotFather",
    )
    allowed_telegram_users: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        alias="ALLOWED_TELEGRAM_USERS",
        description="Allowlist of Telegram user IDs permitted to interact with the bot",
    )
    telegram_proxy: str | None = Field(
        default=None,
        alias="TELEGRAM_PROXY",
        description="Optional HTTP or SOCKS5 proxy URL for Telegram Bot API",
    )

    # 2. Workspace and CLI settings
    allowed_workspaces: Annotated[dict[str, Path], NoDecode] = Field(
        default_factory=dict,
        alias="ALLOWED_WORKSPACES",
        description="Mapping of workspace aliases to absolute directory paths",
    )
    antigravity_cli_path: str = Field(
        default="agy",
        alias="ANTIGRAVITY_CLI_PATH",
        description="Path or command name for Antigravity CLI",
    )
    task_execution_timeout_seconds: int = Field(
        default=300,
        alias="TASK_EXECUTION_TIMEOUT_SECONDS",
        description="Maximum execution time for a CLI task in seconds",
    )
    confirmation_timeout_seconds: int = Field(
        default=120,
        alias="CONFIRMATION_TIMEOUT_SECONDS",
        description="Timeout in seconds for interactive confirmation of /run tasks",
    )
    cli_auto_approve_permissions: bool = Field(
        default=True,
        alias="CLI_AUTO_APPROVE_PERMISSIONS",
        description="Auto-approve tool permissions in non-interactive CLI executions",
    )
    cli_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        default="low",
        alias="CLI_REASONING_EFFORT",
        description="Reasoning effort for Antigravity CLI (low, medium, high, xhigh, max)",
    )

    # 3. Wake-on-LAN settings
    target_mac_address: str | None = Field(
        default=None,
        alias="TARGET_MAC_ADDRESS",
        description="MAC address of target PC network adapter (e.g. AA:BB:CC:DD:EE:FF)",
    )
    wol_broadcast_ip: str = Field(
        default="255.255.255.255",
        alias="WOL_BROADCAST_IP",
        description="Broadcast IP address for Magic Packet transmission",
    )
    wol_port: int = Field(
        default=9,
        alias="WOL_PORT",
        description="UDP port for Wake-on-LAN transmission",
    )

    # 4. Controller & Agent settings
    controller_mode: Literal["local", "distributed"] = Field(
        default="local",
        alias="CONTROLLER_MODE",
        description="Deployment mode: local (dev on single PC) or distributed (separate hosts)",
    )
    controller_host: str = Field(
        default="127.0.0.1",
        alias="CONTROLLER_HOST",
        description="Host address for Controller service",
    )
    controller_port: int = Field(
        default=8080,
        alias="CONTROLLER_PORT",
        description="Port for Controller service",
    )
    agent_auth_secret: SecretStr | None = Field(
        default=None,
        alias="AGENT_AUTH_SECRET",
        description="Pre-shared secret key for Controller <-> Windows Agent authentication",
    )

    # 5. Task queue and SQLite settings
    sqlite_database_path: Path = Field(
        default=Path("data/tasks.sqlite3"),
        alias="SQLITE_DATABASE_PATH",
        description="Path to SQLite database file for task history and queue",
    )
    task_results_dir: Path = Field(
        default=Path("data/results"),
        alias="TASK_RESULTS_DIR",
        description="Path to directory for saving long execution results",
    )

    # -------------------------------------------------------------------------
    # Validators
    # -------------------------------------------------------------------------

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _validate_telegram_bot_token(cls, v: Any) -> SecretStr | None:
        if v is None or v == "":
            return None
        raw_val = v.get_secret_value() if isinstance(v, SecretStr) else str(v).strip()
        if not raw_val:
            return None
        if not _TELEGRAM_TOKEN_REGEX.match(raw_val):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN has invalid format. Expected '<bot_id>:<secret>'."
            )
        return SecretStr(raw_val)

    @field_validator("telegram_proxy", mode="before")
    @classmethod
    def _validate_telegram_proxy(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        v_str = str(v).strip()
        if not v_str:
            return None
        valid_schemes = ("http://", "https://", "socks5://", "socks5h://", "socks4://")
        if not any(v_str.lower().startswith(s) for s in valid_schemes):
            raise ValueError(
                f"TELEGRAM_PROXY has unsupported scheme: '{v_str}'. "
                "Expected http://, https://, socks5://, socks5h:// or socks4://"
            )
        return v_str

    @field_validator("allowed_telegram_users", mode="before")
    @classmethod
    def _parse_allowed_users(cls, v: Any) -> list[int]:
        if v is None or v == "":
            return []
        if isinstance(v, list):
            items = v
        elif isinstance(v, set):
            items = list(v)
        elif isinstance(v, str):
            v_str = v.strip().strip("\"'")
            if not v_str:
                return []
            if v_str.startswith("["):
                try:
                    parsed = json.loads(v_str)
                    items = parsed if isinstance(parsed, list) else [parsed]
                except Exception as e:
                    raise ValueError(f"Failed to parse ALLOWED_TELEGRAM_USERS JSON: {e}") from e
            else:
                items = [item.strip() for item in v_str.split(",") if item.strip()]
        else:
            items = [v]

        result: list[int] = []
        for item in items:
            try:
                user_id = int(item)
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Invalid Telegram user ID '{item}'. Expected a positive integer."
                ) from e
            if user_id <= 0:
                raise ValueError(
                    f"Invalid Telegram user ID '{user_id}'. Must be a positive integer."
                )
            if user_id not in result:
                result.append(user_id)
        return result

    @field_validator("allowed_workspaces", mode="before")
    @classmethod
    def _parse_allowed_workspaces(cls, v: Any) -> dict[str, Path]:
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            raw_map = v
        elif isinstance(v, str):
            v_str = v.strip().strip("\"'")
            if not v_str:
                return {}
            if v_str.startswith("{"):
                try:
                    parsed = json.loads(v_str)
                    if not isinstance(parsed, dict):
                        raise ValueError("ALLOWED_WORKSPACES JSON must be an object (mapping).")
                    raw_map = parsed
                except json.JSONDecodeError as e:
                    raise ValueError(f"Failed to parse ALLOWED_WORKSPACES JSON: {e}") from e
            else:
                raw_map = {}
                pairs = [p.strip() for p in v_str.split(",") if p.strip()]
                for pair in pairs:
                    if "=" not in pair:
                        raise ValueError(
                            f"Invalid workspace pair '{pair}'. Expected format 'alias=path'."
                        )
                    alias, path_str = pair.split("=", 1)
                    raw_map[alias.strip()] = path_str.strip()
        else:
            raise ValueError("ALLOWED_WORKSPACES must be a dictionary or JSON string.")

        validated: dict[str, Path] = {}
        for alias, raw_path in raw_map.items():
            clean_alias = str(alias).strip()
            if not _ALIAS_REGEX.match(clean_alias):
                raise ValueError(
                    f"Invalid workspace alias '{clean_alias}'. "
                    "Must contain only letters, numbers, hyphens, and underscores."
                )

            path_str = str(raw_path).strip()
            if ".." in path_str:
                raise ValueError(
                    f"Workspace path for '{clean_alias}' contains "
                    f"forbidden traversal sequence ('..'): {path_str}"
                )

            if not os.path.isabs(path_str):
                raise ValueError(
                    f"Workspace path for '{clean_alias}' must be an absolute path: '{path_str}'"
                )

            resolved_path = Path(path_str).resolve()
            validated[clean_alias] = resolved_path

        return validated

    @field_validator("task_execution_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if v < 10 or v > 86400:
            raise ValueError(
                f"TASK_EXECUTION_TIMEOUT_SECONDS must be between 10 and 86400 seconds (got {v})."
            )
        return v

    @field_validator("confirmation_timeout_seconds")
    @classmethod
    def _validate_confirmation_timeout(cls, v: int) -> int:
        if v < 10 or v > 600:
            raise ValueError(
                f"CONFIRMATION_TIMEOUT_SECONDS must be between 10 and 600 seconds (got {v})."
            )
        return v

    @field_validator("target_mac_address", mode="before")
    @classmethod
    def _validate_mac(cls, v: Any) -> str | None:
        if v is None or v == "":
            return None
        v_str = str(v).strip()
        if not v_str:
            return None
        if not _MAC_REGEX.match(v_str):
            raise ValueError(
                f"TARGET_MAC_ADDRESS has invalid format: '{v_str}'. "
                "Expected format 'AA:BB:CC:DD:EE:FF' or 'AA-BB-CC-DD-EE-FF'."
            )
        return v_str.replace("-", ":").upper()

    @field_validator("wol_broadcast_ip")
    @classmethod
    def _validate_broadcast_ip(cls, v: str) -> str:
        clean_ip = v.strip()
        try:
            ipaddress.IPv4Address(clean_ip)
        except ValueError as e:
            raise ValueError(f"WOL_BROADCAST_IP is not a valid IPv4 address: '{clean_ip}'") from e
        return clean_ip

    @field_validator("wol_port")
    @classmethod
    def _validate_wol_port(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError(f"WOL_PORT must be between 1 and 65535 (got {v}).")
        return v

    @field_validator("controller_port")
    @classmethod
    def _validate_controller_port(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError(f"CONTROLLER_PORT must be between 1 and 65535 (got {v}).")
        return v

    @field_validator("agent_auth_secret", mode="before")
    @classmethod
    def _validate_agent_auth_secret(cls, v: Any) -> SecretStr | None:
        if v is None or v == "":
            return None
        raw_val = v.get_secret_value() if isinstance(v, SecretStr) else str(v).strip()
        if not raw_val:
            return None
        if len(raw_val) < 16:
            raise ValueError(
                "AGENT_AUTH_SECRET must be at least 16 characters long for secure authentication."
            )
        return SecretStr(raw_val)

    @field_validator("sqlite_database_path", mode="before")
    @classmethod
    def _validate_sqlite_path(cls, v: Any) -> Path:
        if v is None or v == "":
            return Path("data/tasks.sqlite3").resolve()
        return Path(v).resolve()

    @field_validator("task_results_dir", mode="before")
    @classmethod
    def _validate_task_results_dir(cls, v: Any) -> Path:
        if v is None or v == "":
            return Path("data/results").resolve()
        return Path(v).resolve()

    # -------------------------------------------------------------------------
    # Capability flags
    # -------------------------------------------------------------------------

    @property
    def is_telegram_enabled(self) -> bool:
        """Indicates whether Telegram Bot can be initialized."""
        return bool(self.telegram_bot_token and self.allowed_telegram_users)

    @property
    def is_wol_enabled(self) -> bool:
        """Indicates whether Wake-on-LAN dispatching is configured and available."""
        return bool(self.target_mac_address)

    @property
    def is_task_execution_enabled(self) -> bool:
        """Indicates whether task execution in allowed workspaces is available."""
        return bool(self.allowed_workspaces)

    # -------------------------------------------------------------------------
    # Diagnostic summary (secrets stripped)
    # -------------------------------------------------------------------------

    def safe_summary(self) -> dict[str, Any]:
        """Returns a non-sensitive summary of active settings safe for logging and diagnostics."""
        return {
            "telegram_configured": self.is_telegram_enabled,
            "telegram_proxy_configured": bool(self.telegram_proxy),
            "allowed_users_count": len(self.allowed_telegram_users),
            "workspaces_count": len(self.allowed_workspaces),
            "workspaces_aliases": list(self.allowed_workspaces.keys()),
            "antigravity_cli_path": self.antigravity_cli_path,
            "task_timeout_seconds": self.task_execution_timeout_seconds,
            "confirmation_timeout_seconds": self.confirmation_timeout_seconds,
            "cli_auto_approve_permissions": self.cli_auto_approve_permissions,
            "cli_reasoning_effort": self.cli_reasoning_effort,
            "sqlite_database_path": str(self.sqlite_database_path),
            "task_results_dir": str(self.task_results_dir),
            "wol_enabled": self.is_wol_enabled,
            "target_mac": self.target_mac_address,
            "wol_broadcast_ip": self.wol_broadcast_ip,
            "wol_port": self.wol_port,
            "controller_mode": self.controller_mode,
            "controller_host": self.controller_host,
            "controller_port": self.controller_port,
            "agent_auth_configured": self.agent_auth_secret is not None,
        }


def load_config(env_file: str | Path | None = None, **overrides: Any) -> AppConfig:
    """Load and validate application configuration.

    Args:
        env_file: Optional path to a specific .env file.
        **overrides: Explicit keyword arguments to override environment values.

    Returns:
        Validated AppConfig instance.
    """
    if env_file is not None:
        return AppConfig(_env_file=str(env_file), **overrides)
    return AppConfig(**overrides)
