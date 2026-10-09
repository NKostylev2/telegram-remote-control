"""Unit tests for configuration validation and secret management."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from remote_control.config import AppConfig, load_config


class TestValidConfiguration:
    """Tests for successfully loading valid configurations."""

    def test_full_valid_configuration(self, valid_config_dict: dict[str, Any]) -> None:
        """Full configuration parses correctly and enables all capabilities."""
        config = load_config(**valid_config_dict)

        assert config.telegram_bot_token is not None
        expected_token = valid_config_dict["TELEGRAM_BOT_TOKEN"]
        assert config.telegram_bot_token.get_secret_value() == expected_token
        assert config.allowed_telegram_users == [111222333, 444555666]
        assert "test_proj" in config.allowed_workspaces
        assert isinstance(config.allowed_workspaces["test_proj"], Path)
        assert config.target_mac_address == "AA:BB:CC:DD:EE:FF"
        assert config.wol_broadcast_ip == "192.168.1.255"
        assert config.wol_port == 9
        assert config.task_execution_timeout_seconds == 300
        assert config.controller_mode == "local"
        assert config.controller_host == "127.0.0.1"
        assert config.controller_port == 8080
        assert config.agent_auth_secret is not None
        expected_secret = valid_config_dict["AGENT_AUTH_SECRET"]
        assert config.agent_auth_secret.get_secret_value() == expected_secret

        # Capability flags
        assert config.is_telegram_enabled is True
        assert config.is_wol_enabled is True
        assert config.is_task_execution_enabled is True

    def test_mac_normalization(self, valid_config_dict: dict[str, Any]) -> None:
        """Hyphenated lowercase MAC is normalized to colon uppercase."""
        valid_config_dict["TARGET_MAC_ADDRESS"] = "00-11-22-33-44-55"
        config = load_config(**valid_config_dict)
        assert config.target_mac_address == "00:11:22:33:44:55"


class TestEmptyAndMissingParameters:
    """Tests for empty defaults and unconfigured states."""

    def test_empty_config_does_not_crash(self, clean_env: None) -> None:
        """Importing or instantiating empty config does not raise and disables features."""
        config = AppConfig()

        assert config.telegram_bot_token is None
        assert config.allowed_telegram_users == []
        assert config.allowed_workspaces == {}
        assert config.target_mac_address is None

        # Features MUST NOT be activated without parameters
        assert config.is_telegram_enabled is False
        assert config.is_wol_enabled is False
        assert config.is_task_execution_enabled is False

    def test_no_fake_workspaces_or_mac_substituted(self, clean_env: None) -> None:
        """Default configuration must not substitute fake paths or MAC addresses."""
        config = AppConfig()
        assert len(config.allowed_workspaces) == 0
        assert config.target_mac_address is None


class TestInputFormatsAndParsing:
    """Tests for parsing various input formats (JSON strings vs comma-separated)."""

    def test_allowed_users_from_comma_string(self) -> None:
        config = AppConfig(ALLOWED_TELEGRAM_USERS="100, 200, 300")
        assert config.allowed_telegram_users == [100, 200, 300]

    def test_allowed_users_from_json_string(self) -> None:
        config = AppConfig(ALLOWED_TELEGRAM_USERS="[555, 777]")
        assert config.allowed_telegram_users == [555, 777]

    def test_workspaces_from_json_string(self, tmp_path: Path) -> None:
        p1 = tmp_path / "proj1"
        p2 = tmp_path / "proj2"
        p1.mkdir()
        p2.mkdir()
        json_str = f'{{"repo1": "{p1.as_posix()}", "repo2": "{p2.as_posix()}"}}'
        config = AppConfig(ALLOWED_WORKSPACES=json_str)
        assert "repo1" in config.allowed_workspaces
        assert "repo2" in config.allowed_workspaces

    def test_workspaces_from_comma_pairs(self, tmp_path: Path) -> None:
        p1 = tmp_path / "proj1"
        p1.mkdir()
        comma_str = f"repo1={p1.as_posix()}"
        config = AppConfig(ALLOWED_WORKSPACES=comma_str)
        assert "repo1" in config.allowed_workspaces

    def test_dotenv_source_comma_separated_users_and_workspaces(self, tmp_path: Path) -> None:
        """DotEnvSettingsSource parses comma-separated users and workspaces without JSON error."""
        proj_dir = tmp_path / "my_proj"
        proj_dir.mkdir()
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345\n"
            f"ALLOWED_TELEGRAM_USERS=123456789,987654321\n"
            f"ALLOWED_WORKSPACES=backend={proj_dir.as_posix()}\n",
            encoding="utf-8",
        )
        config = load_config(env_file=env_file)
        assert config.allowed_telegram_users == [123456789, 987654321]
        assert "backend" in config.allowed_workspaces


class TestValidationErrors:
    """Tests for invalid parameter validation."""

    def test_invalid_telegram_token_format(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(TELEGRAM_BOT_TOKEN="invalid_plain_string")
        assert "TELEGRAM_BOT_TOKEN has invalid format" in str(exc_info.value)

    def test_invalid_user_id_non_numeric(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(ALLOWED_TELEGRAM_USERS="123, abc, 456")
        assert "Invalid Telegram user ID 'abc'" in str(exc_info.value)

    def test_invalid_user_id_negative(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(ALLOWED_TELEGRAM_USERS="-12345")
        assert "Must be a positive integer" in str(exc_info.value)

    def test_workspace_relative_path_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(ALLOWED_WORKSPACES={"proj": "relative/path"})
        assert "must be an absolute path" in str(exc_info.value)

    def test_workspace_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(ALLOWED_WORKSPACES={"proj": "C:\\projects\\..\\secret"})
        assert "forbidden traversal sequence" in str(exc_info.value)

    def test_workspace_invalid_alias_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(ALLOWED_WORKSPACES={"bad alias with spaces!": str(tmp_path)})
        assert "Invalid workspace alias" in str(exc_info.value)

    @pytest.mark.parametrize("timeout", [0, 5, 9, 86401, -100])
    def test_invalid_task_timeout(self, timeout: int) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(TASK_EXECUTION_TIMEOUT_SECONDS=timeout)
        assert "TASK_EXECUTION_TIMEOUT_SECONDS must be between 10 and 86400" in str(exc_info.value)

    @pytest.mark.parametrize("timeout", [0, 5, 9, 601, -100])
    def test_invalid_confirmation_timeout(self, timeout: int) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(CONFIRMATION_TIMEOUT_SECONDS=timeout)
        assert "CONFIRMATION_TIMEOUT_SECONDS must be between 10 and 600" in str(exc_info.value)

    def test_invalid_telegram_proxy_scheme(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(TELEGRAM_PROXY="ftp://127.0.0.1:1080")
        assert "TELEGRAM_PROXY has unsupported scheme" in str(exc_info.value)

    def test_valid_telegram_proxy_schemes(self) -> None:
        for p in ["http://127.0.0.1:8080", "socks5://localhost:1080", "socks5h://127.0.0.1:1080"]:
            c = AppConfig(TELEGRAM_PROXY=p)
            assert c.telegram_proxy == p

    @pytest.mark.parametrize("port", [0, -1, 65536, 70000])
    def test_invalid_wol_port(self, port: int) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(WOL_PORT=port)
        assert "WOL_PORT must be between 1 and 65535" in str(exc_info.value)

    @pytest.mark.parametrize(
        "mac",
        ["00:11:22", "GG:11:22:33:44:55", "00-11-22-33-44-55-66", "not-a-mac"],
    )
    def test_invalid_mac_address(self, mac: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(TARGET_MAC_ADDRESS=mac)
        assert "TARGET_MAC_ADDRESS has invalid format" in str(exc_info.value)

    def test_invalid_broadcast_ip(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(WOL_BROADCAST_IP="999.999.999.999")
        assert "WOL_BROADCAST_IP is not a valid IPv4 address" in str(exc_info.value)

    def test_agent_auth_secret_too_short(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppConfig(AGENT_AUTH_SECRET="short_secret")
        assert "AGENT_AUTH_SECRET must be at least 16 characters" in str(exc_info.value)


class TestSecretProtection:
    """Tests ensuring secrets are NEVER leaked in logs, summaries, or str/repr."""

    def test_secrets_masked_in_str_and_repr(self, valid_config_dict: dict[str, Any]) -> None:
        config = load_config(**valid_config_dict)
        raw_token = valid_config_dict["TELEGRAM_BOT_TOKEN"]
        raw_secret = valid_config_dict["AGENT_AUTH_SECRET"]

        # Ensure raw secret is not in str() or repr()
        assert raw_token not in str(config)
        assert raw_token not in repr(config)
        assert raw_secret not in str(config)
        assert raw_secret not in repr(config)

    def test_safe_summary_does_not_contain_secrets(
        self, valid_config_dict: dict[str, Any]
    ) -> None:
        config = load_config(**valid_config_dict)
        summary = config.safe_summary()

        summary_str = str(summary)
        raw_token = valid_config_dict["TELEGRAM_BOT_TOKEN"]
        raw_secret = valid_config_dict["AGENT_AUTH_SECRET"]

        assert raw_token not in summary_str
        assert raw_secret not in summary_str
        assert summary["telegram_configured"] is True
        assert summary["agent_auth_configured"] is True
