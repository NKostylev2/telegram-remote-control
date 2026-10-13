"""Unit tests for Antigravity CLI adapter (remote_control.agent.agy_client).

All tests use mocks for external processes and file system calls to guarantee:
- Completely offline execution.
- No real CLI process spawning.
- Independence from user authentication state.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from remote_control.agent.agy_client import (
    AgyClient,
    CliNotFoundError,
    JsonParseError,
    WorkspaceDeniedError,
    WorkspaceInvalidError,
    WorkspaceNotFoundError,
)
from remote_control.common.models import TaskRequest, TaskResult, TaskStatus
from remote_control.config import AppConfig


@pytest.fixture
def mock_cli_path(tmp_path: Path) -> Path:
    """Create a dummy executable file for mock testing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_exe = bin_dir / "agy.exe"
    fake_exe.write_text("dummy binary", encoding="utf-8")
    return fake_exe


@pytest.fixture
def mock_workspace(tmp_path: Path) -> Path:
    """Create an allowed workspace directory."""
    ws = tmp_path / "workspaces" / "project1"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def sample_config(mock_cli_path: Path, mock_workspace: Path) -> AppConfig:
    """Create a valid AppConfig fixture for agent client testing."""
    return AppConfig(
        TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz12345",
        ALLOWED_TELEGRAM_USERS=[111222333],
        ALLOWED_WORKSPACES={"project1": str(mock_workspace)},
        ANTIGRAVITY_CLI_PATH=str(mock_cli_path),
        TASK_EXECUTION_TIMEOUT_SECONDS=60,
    )


class TestCliPathResolution:
    """Tests for locating the agy binary safely."""

    def test_direct_valid_path(self, sample_config: AppConfig, mock_cli_path: Path) -> None:
        client = AgyClient(sample_config)
        assert client.get_cli_path() == mock_cli_path.resolve()
        assert client.is_cli_available() is True

    def test_shutil_which_fallback(self, tmp_path: Path, mock_workspace: Path) -> None:
        fake_binary = tmp_path / "custom_agy.exe"
        fake_binary.write_text("dummy", encoding="utf-8")

        config = AppConfig(
            ALLOWED_WORKSPACES={"project1": str(mock_workspace)},
            ANTIGRAVITY_CLI_PATH="custom_agy",
        )
        client = AgyClient(config)

        with patch("shutil.which", return_value=str(fake_binary)):
            resolved = client.get_cli_path()
            assert resolved == fake_binary.resolve()
            assert client.is_cli_available() is True

    def test_missing_binary_raises_cli_not_found(self, mock_workspace: Path) -> None:
        config = AppConfig(
            ALLOWED_WORKSPACES={"project1": str(mock_workspace)},
            ANTIGRAVITY_CLI_PATH="C:/nonexistent/path/to/agy.exe",
        )
        client = AgyClient(config)

        with patch("shutil.which", return_value=None), patch.dict("os.environ", {}, clear=True):
            with pytest.raises(CliNotFoundError) as exc_info:
                client.get_cli_path()
            assert "Antigravity CLI executable was not found" in str(exc_info.value)
            assert client.is_cli_available() is False


class TestCommandArgsConstruction:
    """Tests for CLI arguments generation."""

    def test_build_args_minimal(self, sample_config: AppConfig, mock_cli_path: Path) -> None:
        client = AgyClient(sample_config)
        args = client.build_command_args(prompt="Summarize repo")

        # Auto-approval of permissions is enabled by default to allow command execution
        expected = [
            str(mock_cli_path.resolve()),
            "--print",
            "Summarize repo",
            "--output-format",
            "json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--effort",
            "low",
        ]
        assert args == expected

        # Can be explicitly disabled
        args_disabled = client.build_command_args(
            prompt="Summarize repo", dangerously_skip_permissions=False
        )
        assert "--dangerously-skip-permissions" not in args_disabled

    def test_build_args_with_auto_approve_permissions(
        self, sample_config: AppConfig, mock_cli_path: Path
    ) -> None:
        client = AgyClient(sample_config)
        args = client.build_command_args(
            prompt="Summarize repo",
            dangerously_skip_permissions=True,
        )
        assert "--dangerously-skip-permissions" in args

    def test_build_args_with_timeout_and_model(
        self, sample_config: AppConfig, mock_cli_path: Path
    ) -> None:
        client = AgyClient(sample_config)
        args = client.build_command_args(
            prompt="Refactor code",
            timeout_seconds=45,
            model="gemini-3.8-flash-high",
        )

        assert "--print-timeout" in args
        timeout_idx = args.index("--print-timeout")
        assert args[timeout_idx + 1] == "45s"

        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "gemini-3.8-flash-high"

    def test_build_args_concise_mode(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        # Action task concise guidance
        args = client.build_command_args(prompt="Run test", concise=True)
        print_idx = args.index("--print")
        prompt_arg = args[print_idx + 1]
        assert "Выполни задачу: Run test" in prompt_arg
        assert "Ответь предельно кратко 1 предложением" in prompt_arg

        # Query task concise guidance
        query_args = client.build_command_args(prompt="Покажи статус", concise=True)
        query_prompt_arg = query_args[query_args.index("--print") + 1]
        assert "в виде таблицы или компактного списка" in query_prompt_arg

    def test_empty_prompt_rejected(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(ValueError, match="Prompt cannot be empty"):
            client.build_command_args("   ")

    def test_null_byte_in_prompt_rejected(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(ValueError, match="null byte"):
            client.build_command_args("Dangerous \x00 prompt")

    def test_oversized_prompt_rejected(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(ValueError, match="exceeds maximum allowed length"):
            client.build_command_args("a" * 50001)


class TestWorkspaceValidation:
    """Tests for workspace allowlist checks and boundary traversal protection."""

    def test_valid_workspace_resolves(
        self, sample_config: AppConfig, mock_workspace: Path
    ) -> None:
        client = AgyClient(sample_config)
        resolved = client.resolve_workspace("project1")
        assert resolved == mock_workspace.resolve()

    def test_unregistered_workspace_denied(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(WorkspaceDeniedError, match="not permitted"):
            client.resolve_workspace("unauthorized_project")

    def test_nonexistent_workspace_directory(
        self, sample_config: AppConfig, mock_workspace: Path
    ) -> None:
        client = AgyClient(sample_config)
        # Delete directory after configuration
        mock_workspace.rmdir()
        with pytest.raises(WorkspaceNotFoundError, match="does not exist"):
            client.resolve_workspace("project1")

    def test_workspace_pointing_to_file(self, tmp_path: Path, mock_cli_path: Path) -> None:
        fake_file = tmp_path / "not_a_dir.txt"
        fake_file.write_text("file content", encoding="utf-8")

        config = AppConfig(
            ALLOWED_WORKSPACES={"file_proj": str(fake_file)},
            ANTIGRAVITY_CLI_PATH=str(mock_cli_path),
        )
        client = AgyClient(config)
        with pytest.raises(WorkspaceInvalidError, match="not a directory"):
            client.resolve_workspace("file_proj")


class TestOutputParsing:
    """Tests for JSON and stderr parsing routines."""

    def test_parse_successful_json_envelope(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        raw_output = json.dumps(
            {
                "conversation_id": "test-conv-123",
                "status": "SUCCESS",
                "response": "AGY_SMOKE_OK\n",
                "duration_seconds": 1.25,
                "num_turns": 1,
                "usage": {"total_tokens": 500},
            }
        )

        parsed = client._parse_stdout_json(raw_output)
        assert parsed["status"] == "SUCCESS"
        assert parsed["conversation_id"] == "test-conv-123"
        assert parsed["response"] == "AGY_SMOKE_OK\n"

    def test_parse_embedded_json_in_stdout(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        raw_output = (
            "Loading agent...\n"
            "{\"status\": \"SUCCESS\", \"response\": \"done\"}\n"
            "Finished."
        )
        parsed = client._parse_stdout_json(raw_output)
        assert parsed["status"] == "SUCCESS"
        assert parsed["response"] == "done"

    def test_parse_invalid_json_raises(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(JsonParseError, match="could not be parsed as JSON"):
            client._parse_stdout_json("Internal Error: process failed completely")

    def test_parse_empty_stdout_raises(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        with pytest.raises(JsonParseError, match="empty standard output"):
            client._parse_stdout_json("   \n  ")

    def test_parse_stderr_diagnostics(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        stderr_sample = (
            "Fetching available models...\n"
            "Warning: API response was slow\n"
            "Warning: Tool 'file_write' was soft-denied by user policy\n"
            "soft-denied: run_command\n"
        )
        warnings, errors, soft_denied = client._parse_stderr(stderr_sample)

        assert "Warning: API response was slow" in warnings
        assert "file_write" in soft_denied
        assert "run_command" in soft_denied
        assert len(errors) == 0

    def test_evaluate_result_denied_actions(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        raw_json = json.dumps(
            {
                "status": "SUCCESS",
                "response": "",
                "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
            }
        )
        res = client._evaluate_result(
            task_id=uuid4(),
            exit_code=0,
            stdout=raw_json,
            stderr="",
            duration_seconds=1.0,
        )
        assert res.status == TaskStatus.PARTIAL_SUCCESS
        assert "RunCommand" in res.soft_denied_tools
        assert "отклонено политикой прав" in res.response

    def test_evaluate_result_empty_response_fallback(self, sample_config: AppConfig) -> None:
        client = AgyClient(sample_config)
        raw_json = json.dumps({"status": "SUCCESS", "response": ""})
        res = client._evaluate_result(
            task_id=uuid4(),
            exit_code=0,
            stdout=raw_json,
            stderr="",
            duration_seconds=1.0,
        )
        assert res.status == TaskStatus.SUCCESS
        assert "успешно выполнено на хосте" in res.response


class TestExecuteSubprocessWithMocks:
    """Async execution tests using mocked subprocesses."""

    def test_successful_execution(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            tid = uuid4()

            mock_payload = json.dumps(
                {
                    "conversation_id": "conv-456",
                    "status": "SUCCESS",
                    "response": "Refactoring completed successfully.",
                    "duration_seconds": 2.5,
                    "usage": {"total_tokens": 1200},
                }
            ).encode("utf-8")

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(return_value=(mock_payload, b""))

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await client.execute(
                    prompt="Do work",
                    workspace_alias="project1",
                    task_id=tid,
                )

            assert result.task_id == tid
            assert result.status == TaskStatus.SUCCESS
            assert result.exit_code == 0
            assert result.response == "Refactoring completed successfully."
            assert result.conversation_id == "conv-456"
            assert result.error == ""
            assert result.raw_usage["total_tokens"] == 1200

        asyncio.run(_test())

    def test_execution_passes_workspace_as_cwd(
        self, sample_config: AppConfig, mock_workspace: Path
    ) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(
                return_value=(b'{"status": "SUCCESS", "response": "ok"}', b"")
            )

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
                await client.execute(
                    prompt="Test cwd",
                    workspace_alias="project1",
                )

            mock_exec.assert_called_once()
            assert mock_exec.call_args.kwargs.get("cwd") == str(mock_workspace.resolve())

        asyncio.run(_test())

    def test_non_zero_exit_code_returns_error(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_proc = MagicMock()
            mock_proc.returncode = 1
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(
                return_value=(b"", b"Error: unauthorized or missing API token\n")
            )

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await client.execute(
                    prompt="Do work",
                    workspace_alias="project1",
                )

            assert result.status == TaskStatus.ERROR
            assert result.exit_code == 1
            assert "unauthorized or missing API token" in result.error

        asyncio.run(_test())

    def test_cli_json_error_status(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_payload = json.dumps(
                {
                    "status": "ERROR",
                    "error": "Model quota exceeded.",
                }
            ).encode("utf-8")

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(return_value=(mock_payload, b""))

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await client.execute(
                    prompt="Do work",
                    workspace_alias="project1",
                )

            assert result.status == TaskStatus.ERROR
            assert result.exit_code == 0
            assert "Model quota exceeded." in result.error

        asyncio.run(_test())

    def test_invalid_json_returns_error(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(
                return_value=(b"Random non-JSON text output", b"")
            )

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await client.execute(
                    prompt="Do work",
                    workspace_alias="project1",
                )

            assert result.status == TaskStatus.ERROR
            assert result.exit_code == 0
            assert "could not be parsed as JSON" in result.error
            assert result.response == "Random non-JSON text output"

        asyncio.run(_test())

    def test_soft_denied_tools_sets_partial_success(
        self, sample_config: AppConfig
    ) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_payload = json.dumps(
                {
                    "status": "SUCCESS",
                    "response": "Analysis complete, but changes were not applied.",
                    "soft_denied_tools": ["edit_file"],
                }
            ).encode("utf-8")

            stderr_bytes = b"Warning: Tool 'run_shell' was soft-denied.\n"

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(return_value=(mock_payload, stderr_bytes))

            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await client.execute(
                    prompt="Do work",
                    workspace_alias="project1",
                )

            assert result.status == TaskStatus.PARTIAL_SUCCESS
            assert result.exit_code == 0
            assert "edit_file" in result.soft_denied_tools
            assert "run_shell" in result.soft_denied_tools
            assert len(result.warnings) > 0

        asyncio.run(_test())

    def test_execution_timeout_kills_process(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_proc = MagicMock()
            mock_proc.returncode = None
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock(return_value=0)

            with (
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                patch.object(client, "_terminate_process", new_callable=AsyncMock) as mock_term,
            ):
                result = await client.execute(
                    prompt="Slow query",
                    workspace_alias="project1",
                    timeout_seconds=5,
                )

            mock_term.assert_awaited_once_with(mock_proc)
            assert result.status == TaskStatus.INTERRUPTED
            assert result.exit_code == 124
            assert "timed out after 5.0 seconds" in result.error

        asyncio.run(_test())

    def test_execution_cancelled_kills_process(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_proc = MagicMock()
            mock_proc.returncode = None
            mock_proc.pid = 9999
            mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

            with (
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                patch.object(client, "_terminate_process", new_callable=AsyncMock) as mock_term,
            ):
                result = await client.execute(
                    prompt="Work to cancel",
                    workspace_alias="project1",
                )

            mock_term.assert_awaited_once_with(mock_proc)
            assert result.status == TaskStatus.CANCELED
            assert result.exit_code == 130
            assert "canceled" in result.error

        asyncio.run(_test())

    def test_unauthorized_workspace_returns_error_result(
        self, sample_config: AppConfig
    ) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            result = await client.execute(
                prompt="Work",
                workspace_alias="bad_workspace",
            )
            assert result.status == TaskStatus.ERROR
            assert result.exit_code == 1
            assert "not permitted in configuration" in result.error

        asyncio.run(_test())

    def test_missing_binary_returns_error_result(self, mock_workspace: Path) -> None:
        async def _test() -> None:
            config = AppConfig(
                ALLOWED_WORKSPACES={"project1": str(mock_workspace)},
                ANTIGRAVITY_CLI_PATH="C:/nonexistent/agy.exe",
            )
            client = AgyClient(config)
            with (
                patch("shutil.which", return_value=None),
                patch.dict("os.environ", {}, clear=True),
            ):
                result = await client.execute(
                    prompt="Work",
                    workspace_alias="project1",
                )

            assert result.status == TaskStatus.ERROR
            assert result.exit_code == 1
            assert "executable was not found" in result.error

        asyncio.run(_test())

    def test_execute_task_delegates(self, sample_config: AppConfig) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            task = TaskRequest(
                workspace_alias="project1",
                prompt="Delegate task",
                requested_by=12345,
            )

            mock_result = TaskResult(
                task_id=task.task_id,
                status=TaskStatus.SUCCESS,
                response="Done",
            )

            with patch.object(
                client, "execute", new_callable=AsyncMock, return_value=mock_result
            ):
                res = await client.execute_task(task, timeout_seconds=30)
                assert res == mock_result

        asyncio.run(_test())

    def test_interpret_prompt_success(self, sample_config: AppConfig, tmp_path: Path) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            mock_json = json.dumps(
                {"status": "SUCCESS", "response": "Я планирую запустить программу Steam."}
            )

            with patch.object(
                client,
                "_run_process",
                new_callable=AsyncMock,
                return_value=(0, mock_json, "", 1.0),
            ):
                res = await client.interpret_prompt("запусти стим", tmp_path)
                assert res == "Я планирую запустить программу Steam."

        asyncio.run(_test())

    def test_interpret_prompt_fallback_on_error(
        self, sample_config: AppConfig, tmp_path: Path
    ) -> None:
        async def _test() -> None:
            client = AgyClient(sample_config)
            with patch.object(
                client,
                "_run_process",
                side_effect=RuntimeError("Process failed"),
            ):
                res = await client.interpret_prompt("запусти стим", tmp_path)
                assert "Выполнение задачи в директории" in res
                assert "запусти стим" in res

        asyncio.run(_test())


class TestSpecialCharactersAndSecurity:
    """Tests ensuring special shell characters and multiline text are handled without shell."""

    @pytest.mark.parametrize(
        "special_prompt",
        [
            "Line 1\nLine 2\r\nLine 3 with \ttabs",
            'Prompt with "double quotes" and \'single quotes\'',
            "Command injection attempt: `rm -rf /` && $(whoami) | calc.exe",
            "PowerShell symbols: ; Get-Process | Stop-Process -Force",
            "Windows env vars: %USERPROFILE% and %TEMP%",
            "Unicode and emoji: Привет мир! 🚀 🤖 💻 日本語",
        ],
    )
    def test_special_characters_passed_as_single_arg(
        self, sample_config: AppConfig, special_prompt: str
    ) -> None:
        client = AgyClient(sample_config)
        args = client.build_command_args(prompt=special_prompt)

        # In the argument list, the prompt must be an exact single element following --print
        print_idx = args.index("--print")
        assert args[print_idx + 1] == special_prompt.strip()

        # No shell characters should cause multiple split arguments
        # Includes --dangerously-skip-permissions by default, includes --effort low
        assert len(args) == 9
        assert args[3] == "--output-format"
        assert args[4] == "json"
        assert args[5] == "--disable-slash-commands"
        assert args[6] == "--dangerously-skip-permissions"
        assert args[7] == "--effort"
        assert args[8] == "low"

    def test_build_command_args_with_conversation_and_continue(
        self, sample_config: AppConfig
    ) -> None:
        """Stage 12: Verify conversation_id and continue_session flags."""
        client = AgyClient(sample_config)

        # 1. With conversation_id
        args_conv = client.build_command_args(
            prompt="Hello",
            conversation_id="conv-12345",
        )
        assert "--conversation" in args_conv
        conv_idx = args_conv.index("--conversation")
        assert args_conv[conv_idx + 1] == "conv-12345"
        assert "--continue" not in args_conv

        # 2. With continue_session
        args_cont = client.build_command_args(
            prompt="Hello",
            continue_session=True,
        )
        assert "--continue" in args_cont
        assert "--conversation" not in args_cont

    def test_cli_path_caching_and_invalidation(
        self, sample_config: AppConfig, tmp_path: Path
    ) -> None:
        """Stage 9: Verify CLI path caching and invalidation."""
        dummy_exe = tmp_path / "agy.exe"
        dummy_exe.write_text("dummy")

        sample_config.antigravity_cli_path = str(dummy_exe)
        client = AgyClient(sample_config)

        assert client._cached_cli_path is None
        p1 = client.get_cli_path()
        assert p1 == dummy_exe.resolve()
        assert client._cached_cli_path == dummy_exe.resolve()

        # Cache hit
        with patch.object(Path, "is_file", return_value=True):
            p2 = client.get_cli_path()
            assert p2 == p1

        # Cache invalidation
        client.invalidate_cli_cache()
        assert client._cached_cli_path is None

    def test_read_stream_bounded_truncation_and_draining(self) -> None:
        """Stage 6: Verify bounded stream reader limits output and drains remaining data."""
        from remote_control.agent.agy_client import _read_stream_bounded

        async def _test() -> None:
            # Create a reader with 100KB data
            data = b"X" * 102400
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()

            # Limit to 10KB
            limit = 10240
            result = await _read_stream_bounded(reader, max_bytes=limit)

            assert len(result) > limit  # Contains truncated bytes + notice
            assert result.startswith(b"X" * limit)
            assert "Вывод обрезан".encode() in result

            # Ensure stream was drained
            assert reader.at_eof()

        asyncio.run(_test())

