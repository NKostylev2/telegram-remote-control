"""Antigravity CLI adapter module for executing agent tasks safely.

Provides safe non-interactive invocation of Antigravity CLI (agy.exe) with:
- Strict workspace path validation and boundary enforcement.
- Direct subprocess execution without shell interpretation (no shell injection).
- Safe timeout handling and process termination compatible with Windows.
- Parsing of JSON output envelopes, stderr diagnostics, and soft-denied tools detection.
- Structured TaskResult creation adhering to domain models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from remote_control.agent.classifier import classify_task
from remote_control.common.models import TaskRequest, TaskResult, TaskStatus
from remote_control.config import AppConfig

logger = logging.getLogger(__name__)

# Regular expressions for detecting tool denials and warnings in stderr
_TOOL_DENIED_PATTERNS = (
    re.compile(
        r"(?:tool|Tool)\s+['\"`]?([a-zA-Z0-9_-]+)['\"`]?\s+(?:was\s+)?(?:soft-)?denied",
        re.IGNORECASE,
    ),
    re.compile(
        r"soft-denied(?:\s+tool)?:\s+['\"`]?([a-zA-Z0-9_-]+)['\"`]?",
        re.IGNORECASE,
    ),
    re.compile(
        r"permission\s+(?:was\s+)?denied\s+for\s+(?:tool\s+)?['\"`]?([a-zA-Z0-9_-]+)['\"`]?",
        re.IGNORECASE,
    ),
)

_WARNING_KEYWORDS = ("warning:", "warn:", "soft-denied", "permission denied")
_ERROR_KEYWORDS = ("error:", "fatal:", "exception:", "panic:")


class AgyClientError(Exception):
    """Base exception for Antigravity CLI adapter errors."""


class CliNotFoundError(AgyClientError):
    """Raised when the Antigravity CLI binary cannot be located or executed."""


class WorkspaceDeniedError(AgyClientError):
    """Raised when the requested workspace is not authorized or escapes boundaries."""


class WorkspaceNotFoundError(AgyClientError):
    """Raised when the requested workspace directory does not exist on disk."""


class WorkspaceInvalidError(AgyClientError):
    """Raised when the requested workspace exists but is not a directory."""


class ExecutionTimeoutError(AgyClientError):
    """Raised when CLI task execution exceeds the allotted timeout."""


class ExecutionCancelledError(AgyClientError):
    """Raised when CLI task execution is explicitly canceled."""


class JsonParseError(AgyClientError):
    """Raised when the CLI output does not contain valid JSON."""


class AgyClient:
    """Client adapter for executing tasks using Antigravity CLI."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def get_cli_path(self) -> Path:
        """Resolve and validate the absolute path to the Antigravity CLI executable.

        Order of resolution:
        1. Explicit path from config (if it points to an existing file).
        2. Look up command in system PATH using shutil.which.
        3. Standard Windows fallback: %LOCALAPPDATA%\\agy\\bin\\agy.exe.

        Raises:
            CliNotFoundError: If the binary is not found at any location.
        """
        cli_setting = self.config.antigravity_cli_path.strip()

        # 1. Direct path check
        direct_path = Path(cli_setting)
        if direct_path.is_file():
            return direct_path.resolve()

        # 2. Check system PATH
        which_result = shutil.which(cli_setting)
        if which_result:
            resolved_which = Path(which_result).resolve()
            if resolved_which.is_file():
                return resolved_which

        # 3. Windows standard installation path fallback
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            fallback_path = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if fallback_path.is_file():
                return fallback_path.resolve()

        raise CliNotFoundError(
            f"Antigravity CLI executable was not found. Configured path: '{cli_setting}'"
        )

    def is_cli_available(self) -> bool:
        """Check whether the Antigravity CLI is available on this system."""
        try:
            self.get_cli_path()
            return True
        except CliNotFoundError:
            return False

    def resolve_workspace(self, workspace_alias: str) -> Path:
        """Resolve and validate a workspace alias against the configured allowlist.

        Enforces:
        - Alias presence in allowed_workspaces.
        - Canonical path resolution.
        - Existence and directory checks.
        - Directory traversal and boundary protection.

        Raises:
            WorkspaceDeniedError: If the alias is unknown or attempts boundary escape.
            WorkspaceNotFoundError: If the directory does not exist on disk.
            WorkspaceInvalidError: If the path is not a directory.
        """
        if not workspace_alias or workspace_alias not in self.config.allowed_workspaces:
            raise WorkspaceDeniedError(
                f"Workspace alias '{workspace_alias}' is not permitted in configuration."
            )

        configured_path = self.config.allowed_workspaces[workspace_alias]
        canonical_path = configured_path.resolve()

        if not canonical_path.exists():
            raise WorkspaceNotFoundError(
                f"Workspace directory for alias '{workspace_alias}' "
                f"does not exist: {canonical_path}"
            )

        if not canonical_path.is_dir():
            raise WorkspaceInvalidError(
                f"Workspace path for alias '{workspace_alias}' is not a directory: {canonical_path}"
            )

        # Ensure resolved canonical path matches the configured canonical path
        if str(canonical_path).lower() != str(configured_path.resolve()).lower():
            raise WorkspaceDeniedError(
                f"Resolved workspace path for '{workspace_alias}' escapes boundary."
            )

        return canonical_path

    def build_command_args(
        self,
        prompt: str,
        timeout_seconds: int | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
        disable_slash_commands: bool = True,
        dangerously_skip_permissions: bool | None = None,
        concise: bool = False,
    ) -> list[str]:
        """Construct the argument list for invoking agy.exe non-interactively.

        Does NOT use shell string concatenation. Arguments are passed as individual elements.

        Raises:
            ValueError: If prompt is empty or contains forbidden characters (null bytes).
        """
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("Prompt cannot be empty.")

        if "\x00" in clean_prompt:
            raise ValueError("Prompt contains invalid null byte characters.")

        if len(clean_prompt) > 50000:
            raise ValueError("Prompt exceeds maximum allowed length (50,000 characters).")

        cli_path = self.get_cli_path()
        profile = classify_task(clean_prompt)
        effective_effort = effort if effort is not None else profile.effort

        if concise:
            formatted_prompt = (
                f"Выполни задачу: {clean_prompt}\n"
                "Команды исполняются напрямую в PowerShell (не вызывай powershell -Command).\n"
                "Для запуска оконных/фоновых программ Windows используй исключительно:\n"
                "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                "-Arguments @{CommandLine='\"путь_к_exe\" аргументы'; CurrentDirectory='папка'}\n"
                "(НЕ используй Start-Process, так как дочерние процессы закрываются "
                "при выходе CLI).\n"
                f"{profile.guidance}"
            )
        else:
            formatted_prompt = clean_prompt

        args: list[str] = [
            str(cli_path),
            "--print",
            formatted_prompt,
            "--output-format",
            "json",
        ]

        if timeout_seconds is not None and timeout_seconds > 0:
            args.extend(["--print-timeout", f"{int(timeout_seconds)}s"])

        if disable_slash_commands:
            args.append("--disable-slash-commands")

        if dangerously_skip_permissions is None:
            dangerously_skip_permissions = getattr(
                self.config, "cli_auto_approve_permissions", True
            )

        if dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")

        if effective_effort:
            args.extend(["--effort", effective_effort])

        if model:
            args.extend(["--model", model.strip()])

        return args

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        """Safely terminate a running subprocess and its child processes on Windows."""
        if proc.returncode is not None:
            return

        pid = proc.pid
        logger.warning("Terminating subprocess PID %s...", pid)

        # On Windows, try taskkill /T /F to kill the entire process tree
        if sys.platform == "win32" and pid is not None:
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/F",
                    "/T",
                    "/PID",
                    str(pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
            except Exception as e:
                logger.debug("taskkill invocation failed: %s, falling back to proc.kill()", e)

        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

        try:
            await proc.wait()
        except Exception:
            pass

    async def _run_process(
        self,
        args: list[str],
        cwd: Path,
        timeout: float,
    ) -> tuple[int, str, str, float]:
        """Run the CLI subprocess asynchronously with timeout and cancellation protection."""
        start_time = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            duration = time.monotonic() - start_time
            exit_code = proc.returncode if proc.returncode is not None else 0
            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")
            return exit_code, stdout_str, stderr_str, duration

        except TimeoutError as err:
            duration = time.monotonic() - start_time
            await self._terminate_process(proc)
            raise ExecutionTimeoutError(
                f"Antigravity CLI execution timed out after {timeout:.1f} seconds."
            ) from err

        except asyncio.CancelledError as err:
            duration = time.monotonic() - start_time
            await self._terminate_process(proc)
            raise ExecutionCancelledError("Task execution was canceled.") from err

    def _parse_stdout_json(self, stdout: str) -> dict[str, Any]:
        """Parse JSON response envelope from CLI stdout."""
        clean = stdout.strip()
        if not clean:
            raise JsonParseError("CLI produced empty standard output.")

        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                return parsed
            raise JsonParseError("Parsed JSON is not an object.")
        except json.JSONDecodeError:
            pass

        # Attempt to extract JSON substring between outer braces
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            try:
                candidate = json.loads(clean[start : end + 1])
                if isinstance(candidate, dict):
                    return candidate
            except json.JSONDecodeError as e:
                raise JsonParseError(f"Failed to parse embedded JSON: {e}") from e

        raise JsonParseError(
            f"CLI output could not be parsed as JSON: {clean[:200]!r}"
        )

    def _parse_stderr(
        self, stderr: str
    ) -> tuple[list[str], list[str], list[str]]:
        """Parse stderr for diagnostic warnings, errors, and soft-denied tools.

        Returns:
            (warnings, errors, soft_denied_tools)
        """
        warnings: list[str] = []
        errors: list[str] = []
        soft_denied: list[str] = []

        if not stderr or not stderr.strip():
            return warnings, errors, soft_denied

        for line in stderr.splitlines():
            line_str = line.strip()
            if not line_str:
                continue

            lower_line = line_str.lower()

            # Check for soft-denied / tool permission denials
            for pattern in _TOOL_DENIED_PATTERNS:
                match = pattern.search(line_str)
                if match:
                    tool_name = match.group(1).strip()
                    if tool_name and tool_name not in soft_denied:
                        soft_denied.append(tool_name)

            if any(k in lower_line for k in _ERROR_KEYWORDS):
                errors.append(line_str)
            elif any(k in lower_line for k in _WARNING_KEYWORDS):
                warnings.append(line_str)

        return warnings, errors, soft_denied

    def _evaluate_result(
        self,
        task_id: UUID,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_seconds: float,
    ) -> TaskResult:
        """Evaluate process output into a strongly typed TaskResult.

        Decides final status based on:
        - Subprocess exit code.
        - Parsed JSON envelope status.
        - Soft-denied tool detections from JSON and stderr.
        """
        stderr_warnings, stderr_errors, soft_denied_from_stderr = self._parse_stderr(stderr)

        # 1. Non-zero exit code represents a process-level error
        if exit_code != 0:
            error_msg = stderr.strip() or stdout.strip() or f"CLI exited with code {exit_code}"
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.ERROR,
                exit_code=exit_code,
                response=stdout.strip(),
                error=error_msg,
                duration_seconds=duration_seconds,
                soft_denied_tools=soft_denied_from_stderr,
                warnings=stderr_warnings,
            )

        # 2. Exit code is 0; attempt to parse JSON
        try:
            data = self._parse_stdout_json(stdout)
        except JsonParseError as e:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.ERROR,
                exit_code=0,
                response=stdout.strip(),
                error=str(e),
                duration_seconds=duration_seconds,
                soft_denied_tools=soft_denied_from_stderr,
                warnings=stderr_warnings,
            )

        # 3. Analyze JSON payload
        raw_status = str(data.get("status", "")).upper()
        response_text = str(data.get("response", ""))
        error_text = str(data.get("error", ""))
        conversation_id = data.get("conversation_id")
        raw_usage = data.get("usage", {})
        if not isinstance(raw_usage, dict):
            raw_usage = {}

        # Merge soft-denied tools from JSON and stderr
        json_soft_denied = data.get("soft_denied_tools") or data.get("denied_tools") or []
        if isinstance(json_soft_denied, list):
            for t in json_soft_denied:
                t_str = str(t).strip()
                if t_str and t_str not in soft_denied_from_stderr:
                    soft_denied_from_stderr.append(t_str)

        json_denied_actions = data.get("denied_actions") or []
        if isinstance(json_denied_actions, list):
            for item in json_denied_actions:
                if isinstance(item, dict):
                    action_name = item.get("display_name") or item.get("action")
                    if action_name and str(action_name) not in soft_denied_from_stderr:
                        soft_denied_from_stderr.append(str(action_name))
                elif isinstance(item, str) and item not in soft_denied_from_stderr:
                    soft_denied_from_stderr.append(item)

        # 4. Status mapping
        if raw_status == "ERROR":
            status = TaskStatus.ERROR
            final_error = error_text or "CLI reported ERROR status in JSON response."
        elif raw_status in ("CANCELED", "CANCELLED"):
            status = TaskStatus.CANCELED
            final_error = error_text or "Task execution was canceled by CLI."
        elif raw_status in ("INTERRUPTED", "TIMEOUT"):
            status = TaskStatus.INTERRUPTED
            final_error = error_text or "Task was interrupted by CLI."
        elif raw_status == "SUCCESS" or not raw_status:
            if soft_denied_from_stderr:
                status = TaskStatus.PARTIAL_SUCCESS
                final_error = ""
            else:
                status = TaskStatus.SUCCESS
                final_error = ""
        else:
            status = TaskStatus.ERROR
            final_error = f"Unrecognized CLI status envelope '{raw_status}'."

        if not response_text.strip():
            if soft_denied_from_stderr:
                response_text = (
                    "⚠️ Запрошенное действие было отклонено политикой прав "
                    f"(инструменты: {', '.join(soft_denied_from_stderr)})."
                )
            elif status == TaskStatus.SUCCESS:
                response_text = "Действие успешно выполнено на хосте (текстовый вывод отсутствует)."

        return TaskResult(
            task_id=task_id,
            status=status,
            exit_code=0,
            response=response_text,
            error=final_error,
            duration_seconds=duration_seconds,
            soft_denied_tools=soft_denied_from_stderr,
            raw_usage=raw_usage,
            conversation_id=conversation_id,
            warnings=stderr_warnings,
        )

    async def execute(
        self,
        prompt: str,
        workspace_alias: str | None = None,
        workspace_path: Path | None = None,
        task_id: UUID | None = None,
        timeout_seconds: int | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
        concise: bool = True,
    ) -> TaskResult:
        """Execute an Antigravity CLI task in the specified workspace alias or directory path.

        Catches runtime exceptions and returns a fully structured TaskResult without
        letting unhandled exceptions escape to the caller.
        """
        tid = task_id or uuid4()

        # 1. Validate prompt
        try:
            args = self.build_command_args(
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                model=model,
                effort=effort,
                concise=concise,
            )
        except ValueError as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Invalid prompt: {e}",
                duration_seconds=0.0,
            )
        except CliNotFoundError as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=str(e),
                duration_seconds=0.0,
            )

        # 2. Validate workspace
        try:
            if workspace_path is not None:
                target_cwd = workspace_path.resolve()
                if not target_cwd.exists():
                    raise WorkspaceNotFoundError(
                        f"Target workspace directory does not exist: {target_cwd}"
                    )
                if not target_cwd.is_dir():
                    raise WorkspaceInvalidError(
                        f"Target workspace path is not a directory: {target_cwd}"
                    )
            elif workspace_alias is not None:
                target_cwd = self.resolve_workspace(workspace_alias)
            else:
                raise WorkspaceDeniedError(
                    "Neither workspace_alias nor workspace_path was provided."
                )
        except (WorkspaceDeniedError, WorkspaceNotFoundError, WorkspaceInvalidError) as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=str(e),
                duration_seconds=0.0,
            )

        # 3. Determine execution timeout
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None and timeout_seconds > 0
            else float(self.config.task_execution_timeout_seconds)
        )

        # 4. Run subprocess
        try:
            exit_code, stdout, stderr, duration = await self._run_process(
                args=args,
                cwd=target_cwd,
                timeout=timeout,
            )
        except ExecutionTimeoutError as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.INTERRUPTED,
                exit_code=124,
                error=str(e),
                duration_seconds=timeout,
            )
        except ExecutionCancelledError as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.CANCELED,
                exit_code=130,
                error=str(e),
                duration_seconds=0.0,
            )
        except Exception as e:
            return TaskResult(
                task_id=tid,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Failed to launch CLI subprocess: {e}",
                duration_seconds=0.0,
            )

        # 5. Evaluate result
        return self._evaluate_result(
            task_id=tid,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    async def execute_task(
        self,
        task: TaskRequest,
        timeout_seconds: int | None = None,
        *,
        model: str | None = None,
    ) -> TaskResult:
        """Execute a structured TaskRequest using the CLI adapter."""
        return await self.execute(
            prompt=task.prompt,
            workspace_alias=task.workspace_alias,
            task_id=task.task_id,
            timeout_seconds=timeout_seconds,
            model=model,
        )

    async def interpret_prompt(
        self,
        prompt: str,
        cwd: Path,
        timeout_seconds: float = 20.0,
    ) -> str:
        """Obtain a concise 1-2 sentence interpretation of the task intent using CLI.

        Uses low reasoning effort for fast response. Falls back gracefully to a clean description
        if the CLI is unavailable or times out.
        """
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return "Пустая задача."

        if not self.is_cli_available():
            return f"Выполнение задачи в директории {cwd.name or str(cwd)}: {clean_prompt}"

        instruction = (
            f"Кратко перескажи суть задачи пользователя максимум в 1-2 предложениях: "
            f"'{clean_prompt}' (директория: '{cwd}'). "
            "Ответь просто и лаконично, без вступлений и без лишних рассуждений."
        )

        try:
            cli_path = self.get_cli_path()
            effort = getattr(self.config, "cli_reasoning_effort", "low") or "low"
            args = [
                str(cli_path),
                "--print",
                instruction,
                "--effort",
                effort,
                "--output-format",
                "json",
                "--disable-slash-commands",
            ]
            _exit_code, stdout_str, _stderr_str, _duration = await self._run_process(
                args=args,
                cwd=cwd,
                timeout=timeout_seconds,
            )
            parsed = self._parse_stdout_json(stdout_str)
            resp = str(parsed.get("response", "")).strip()
            if resp:
                lines = [line.strip() for line in resp.splitlines() if line.strip()]
                return " ".join(lines)
        except Exception as e:
            logger.warning("Failed to get agent interpretation for prompt: %s", e)

        return f"Выполнение задачи в директории {cwd.name or str(cwd)}: {clean_prompt}"

