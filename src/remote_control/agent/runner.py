"""Windows Agent background runner process.

Orchestrates agent operation:
- Periodic heartbeat transmission and capability reporting.
- High-frequency task polling (250-500 ms target interval).
- Sequential single-concurrency execution via AgyClient.
- Task status reporting and guaranteed result delivery.
- Desktop screenshot capture on demand.
- Cooperative cancellation propagation and graceful shutdown.
- Resilient reconnection with exponential backoff on network dropouts.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
import time
from pathlib import Path

from remote_control.agent.agy_client import AgyClient
from remote_control.agent.client import AgentHttpClient, AgentUnauthorizedError
from remote_control.agent.connection_monitor import ConnectionMonitor
from remote_control.agent.screenshot import capture_desktop_screenshot, set_keep_display_awake
from remote_control.common.models import TaskResult, TaskStatus
from remote_control.common.protocol import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    AgentTaskAssignment,
    DirectoryBrowseResponse,
    HeartbeatRequest,
    TaskResultSubmission,
    TaskStatusUpdateRequest,
)
from remote_control.config import AppConfig
from remote_control.controller.browser import get_available_drives, list_subdirectories

logger = logging.getLogger(__name__)


class AgentRunner:
    """Core controller for Windows Agent operations and task processing."""

    # Pause between heartbeats. A class attribute so tests can shorten it.
    HEARTBEAT_INTERVAL_SECONDS = 5.0

    def __init__(
        self,
        config: AppConfig,
        client: AgentHttpClient | None = None,
        agy_client: AgyClient | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.config = config
        self.hostname = socket.gethostname()
        self.agent_id = agent_id or f"win-{self.hostname.lower()}"

        secret_str = (
            config.agent_auth_secret.get_secret_value()
            if config.agent_auth_secret
            else None
        )
        base_url = f"http://{config.controller_host}:{config.controller_port}"
        self.client = client or AgentHttpClient(
            base_url=base_url,
            agent_id=self.agent_id,
            secret=secret_str,
        )
        self.agy_client = agy_client or AgyClient(config)

        # Connectivity state per channel; turns silent failures into visible log lines.
        self._heartbeat_monitor = ConnectionMonitor("heartbeat", base_url)
        self._poll_monitor = ConnectionMonitor("poll", base_url)

        self._running = False
        self._current_task_assignment: AgentTaskAssignment | None = None
        self._current_exec_task: asyncio.Task[TaskResult] | None = None
        self._cancel_lock = asyncio.Lock()
        self._task_lock = asyncio.Lock()

    def get_available_workspaces(self) -> dict[str, str]:
        """Collect permitted workspaces that physically exist on this Windows machine."""
        workspaces: dict[str, str] = {}
        for alias, path in self.config.allowed_workspaces.items():
            if path.exists() and path.is_dir():
                workspaces[alias] = str(path.resolve())
        return workspaces

    # -------------------------------------------------------------------------
    # Heartbeat Routine
    # -------------------------------------------------------------------------

    async def _heartbeat_once(self) -> None:
        """Transmit a single heartbeat cycle and handle controller instructions."""
        cli_avail = self.agy_client.is_cli_available()
        workspaces = self.get_available_workspaces()
        drives = get_available_drives()
        current_id = (
            self._current_task_assignment.task_id
            if self._current_task_assignment
            else None
        )

        req = HeartbeatRequest(
            agent_id=self.agent_id,
            hostname=self.hostname,
            platform=platform.system().lower(),
            cli_available=cli_avail,
            available_workspaces=workspaces,
            available_drives=drives,
            current_task_id=current_id,
        )

        resp = await self.client.send_heartbeat(req)

        # Check for remote task cancellation instruction
        if current_id is not None and current_id in resp.cancel_task_ids:
            logger.warning("Controller instructed cancellation for active Task #%d", current_id)
            async with self._cancel_lock:
                if self._current_exec_task and not self._current_exec_task.done():
                    self._current_exec_task.cancel()

        # Check for screenshot capture request
        if resp.screenshot_requested:
            logger.info("Controller requested desktop screenshot. Capturing...")
            await self._capture_and_upload_screenshot()

        # Check for directory browsing request
        if resp.browse_request_id and resp.browse_request_path:
            logger.info("Controller requested directory listing for: %s", resp.browse_request_path)
            await self._handle_browse_request(resp.browse_request_id, resp.browse_request_path)

    async def _handle_browse_request(self, req_id: str, dir_path: str) -> None:
        """Inspect subdirectories on Windows host and report back to Controller."""
        try:
            subdirs = await asyncio.to_thread(list_subdirectories, Path(dir_path))
            subdirs_str = [str(p) for p in subdirs]
            result = DirectoryBrowseResponse(
                request_id=req_id,
                path=dir_path,
                subdirectories=subdirs_str,
            )
        except Exception as e:
            logger.warning("Error browsing directory %s: %s", dir_path, e)
            result = DirectoryBrowseResponse(
                request_id=req_id,
                path=dir_path,
                subdirectories=[],
                error=str(e),
            )
        await self.client.submit_browse_response(result)

    async def _capture_and_upload_screenshot(self) -> None:
        """Capture display and upload to Controller."""
        try:
            res = await asyncio.to_thread(capture_desktop_screenshot)
            if res.error or not res.image_bytes:
                logger.error("Desktop capture failed: %s", res.error)
                return
            success = await self.client.upload_screenshot(res.image_bytes)
            if success:
                logger.info(
                    "Desktop screenshot successfully uploaded (%d bytes).", len(res.image_bytes)
                )
            else:
                logger.warning("Controller rejected or missed screenshot upload.")
        except Exception as e:
            logger.exception("Error capturing or uploading screenshot: %s", e)

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat loop running continuously."""
        while self._running:
            try:
                await self._heartbeat_once()
                self._heartbeat_monitor.success()
            except AgentUnauthorizedError as e:
                self._heartbeat_monitor.unauthorized(e)
            except Exception as e:
                self._heartbeat_monitor.failure(e)
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)

    # -------------------------------------------------------------------------
    # Task Execution Routine
    # -------------------------------------------------------------------------

    async def _execute_assigned_task(self, assignment: AgentTaskAssignment) -> None:
        """Execute a claimed task non-interactively using AgyClient."""
        self._current_task_assignment = assignment
        logger.info(
            "Executing Task #%d in workspace '%s': %s",
            assignment.task_id,
            assignment.workspace_alias,
            assignment.prompt[:80],
        )

        # 1. Notify Controller that task has entered RUNNING status
        try:
            await self.client.update_task_status(
                TaskStatusUpdateRequest(
                    agent_id=self.agent_id,
                    task_id=assignment.task_id,
                    status=TaskStatus.RUNNING,
                    message="Task execution started on Windows Agent.",
                )
            )
        except Exception as e:
            logger.warning(
                "Failed sending RUNNING status update for Task #%d: %s", assignment.task_id, e
            )

        # 2. Execute with cancellation safety
        t0 = time.monotonic()
        ws_path = Path(assignment.workspace_path) if assignment.workspace_path else None
        prevent_display_sleep = getattr(
            self.config, "prevent_display_sleep_during_execution", True
        )
        if prevent_display_sleep:
            set_keep_display_awake(True)

        async with self._cancel_lock:
            self._current_exec_task = asyncio.create_task(
                self.agy_client.execute(
                    prompt=assignment.prompt,
                    workspace_alias=assignment.workspace_alias if not ws_path else None,
                    workspace_path=ws_path,
                    effort=assignment.effort,
                    dangerously_skip_permissions=assignment.auto_approve_permissions,
                )
            )

        try:
            result = await self._current_exec_task
        except asyncio.CancelledError:
            logger.info("Task #%d execution was cancelled.", assignment.task_id)
            result = TaskResult(
                task_id=assignment.task_id,
                status=TaskStatus.CANCELED,
                exit_code=130,
                error="Исполнение задачи на агенте было принудительно отменено.",
                duration_seconds=time.monotonic() - t0,
            )
        except Exception as e:
            logger.exception("Error executing task #%d via AgyClient: %s", assignment.task_id, e)
            result = TaskResult(
                task_id=assignment.task_id,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Ошибка исполнения CLI на агенте: {e}",
                duration_seconds=time.monotonic() - t0,
            )
        finally:
            if prevent_display_sleep:
                set_keep_display_awake(False)
            self._current_exec_task = None
            self._current_task_assignment = None

        # 3. Submit result back to Controller with retries
        submission = TaskResultSubmission(
            agent_id=self.agent_id,
            task_id=assignment.task_id,
            status=result.status,
            exit_code=result.exit_code,
            response=result.response,
            error=result.error,
            duration_seconds=result.duration_seconds or (time.monotonic() - t0),
            soft_denied_tools=result.soft_denied_tools,
            warnings=result.warnings,
            conversation_id=result.conversation_id,
            raw_usage=result.raw_usage,
        )

        for attempt in range(5):
            try:
                sub_resp = await self.client.submit_task_result(submission)
                if sub_resp.acknowledged:
                    logger.info(
                        "Task #%d result successfully acknowledged by Controller.",
                        assignment.task_id,
                    )
                    break
            except Exception as e:
                logger.warning(
                    "Attempt %d: failed to submit Task #%d result to Controller: %s. Retrying...",
                    attempt + 1,
                    assignment.task_id,
                    e,
                )
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Main Polling Loop
    # -------------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll Controller API for tasks with backoff and target latency."""
        poll_interval = getattr(
            self.config, "agent_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
        )
        backoff = poll_interval
        max_backoff = 10.0

        while self._running:
            try:
                # Poll only when not already executing a task (concurrency = 1)
                if self._current_task_assignment is None:
                    resp = await self.client.poll_tasks(max_tasks=1)
                    backoff = poll_interval  # Reset backoff on successful connect
                    self._poll_monitor.success()

                    if resp.tasks:
                        task_to_run = resp.tasks[0]
                        async with self._task_lock:
                            await self._execute_assigned_task(task_to_run)
            except AgentUnauthorizedError as e:
                self._poll_monitor.unauthorized(e)
                await asyncio.sleep(5.0)
            except Exception as e:
                self._poll_monitor.failure(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)
                continue

            await asyncio.sleep(poll_interval)

    # -------------------------------------------------------------------------
    # Lifecycle Management
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the agent background loops."""
        if self._running:
            return
        self._running = True
        logger.info(
            "Starting Windows Agent (ID: %s, Host: %s, Controller: %s:%d)...",
            self.agent_id,
            self.hostname,
            self.config.controller_host,
            self.config.controller_port,
        )

        # Run heartbeat and poll loops concurrently
        await asyncio.gather(
            self._heartbeat_loop(),
            self._poll_loop(),
        )

    async def stop(self) -> None:
        """Gracefully stop agent, cancelling running tasks and closing network sessions."""
        if not self._running:
            return
        logger.info("Stopping Windows Agent...")
        self._running = False

        # Cancel any active running task
        async with self._cancel_lock:
            if self._current_exec_task and not self._current_exec_task.done():
                logger.info("Cancelling active task during agent shutdown.")
                self._current_exec_task.cancel()

        await self.client.close()
        logger.info("Windows Agent stopped.")
