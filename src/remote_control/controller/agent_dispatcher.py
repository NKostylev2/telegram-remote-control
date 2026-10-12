"""Agent dispatcher managing Windows Agent lifecycle, polling, and task delivery.

Maintains in-memory state of connected Windows Agent:
- Heartbeat tracking and offline detection.
- Non-blocking task assignment and result future resolution.
- Remote cancellation coordination.
- Desktop screenshot acquisition.
- Event-driven waiting for agent online transition (used by /wake).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from remote_control.common.models import TaskResult, TaskStatus
from remote_control.common.protocol import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    AgentStatusInfo,
    AgentTaskAssignment,
    HeartbeatRequest,
    HeartbeatResponse,
    PollTasksResponse,
    TaskResultSubmission,
    TaskResultSubmissionResponse,
    TaskStatusUpdateRequest,
    TaskStatusUpdateResponse,
)
from remote_control.config import AppConfig

if TYPE_CHECKING:
    from remote_control.controller.task_store import TaskRecord

logger = logging.getLogger(__name__)


class AgentDispatcher:
    """Coordinates task routing, heartbeats, and status between Controller and Agent."""

    def __init__(
        self,
        config: AppConfig,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.heartbeat_timeout = heartbeat_timeout

        # Connected agent metadata
        self._current_agent: AgentStatusInfo | None = None
        self._last_heartbeat_monotonic: float = 0.0
        self._agent_online_event = asyncio.Event()

        # Task dispatch state
        self._assigned_tasks: dict[int, AgentTaskAssignment] = {}
        self._pending_results: dict[int, asyncio.Future[TaskResult]] = {}
        self._cancel_task_ids: set[int] = set()

        # Screenshot request state
        self._screenshot_requested = False
        self._screenshot_future: asyncio.Future[bytes] | None = None

        self._on_agent_online: Callable[[], None] | None = None
        self._lock = asyncio.Lock()

    def set_on_agent_online(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked whenever the Windows Agent checks in online."""
        self._on_agent_online = callback

    # -------------------------------------------------------------------------
    # Agent State Inspection
    # -------------------------------------------------------------------------

    def is_agent_connected(self) -> bool:
        """Check whether the agent has checked in recently."""
        if self._current_agent is None or self._last_heartbeat_monotonic == 0.0:
            return False
        return (time.monotonic() - self._last_heartbeat_monotonic) < self.heartbeat_timeout

    def is_agent_ready(self) -> bool:
        """Check whether the agent is connected and has Antigravity CLI available."""
        return (
            self.is_agent_connected()
            and self._current_agent is not None
            and self._current_agent.cli_available
        )

    def get_agent_info(self) -> AgentStatusInfo | None:
        """Return the current agent snapshot with updated online status."""
        if self._current_agent is None:
            return None
        online = self.is_agent_connected()
        return self._current_agent.model_copy(update={"is_online": online})

    def get_available_workspaces(self) -> dict[str, str]:
        """Return workspaces reported by the connected Windows Agent, or configured fallback."""
        if (
            self.is_agent_connected()
            and self._current_agent
            and self._current_agent.available_workspaces
        ):
            return self._current_agent.available_workspaces
        return {k: str(v) for k, v in self.config.allowed_workspaces.items()}

    # -------------------------------------------------------------------------
    # Heartbeat Handling
    # -------------------------------------------------------------------------

    async def handle_heartbeat(self, req: HeartbeatRequest) -> HeartbeatResponse:
        """Process heartbeat from Windows Agent and return operational instructions."""
        now_mono = time.monotonic()
        self._last_heartbeat_monotonic = now_mono

        self._current_agent = AgentStatusInfo(
            agent_id=req.agent_id,
            hostname=req.hostname,
            platform=req.platform,
            cli_available=req.cli_available,
            cli_version=req.cli_version,
            available_workspaces=req.available_workspaces,
            last_heartbeat=datetime.now(UTC),
            is_online=True,
            current_task_id=req.current_task_id,
        )

        # Notify any waiters that agent is online (e.g. /wake command)
        self._agent_online_event.set()
        if self._on_agent_online is not None:
            try:
                self._on_agent_online()
            except Exception as e:
                logger.warning("Error in on_agent_online callback: %s", e)

        active_id = req.current_task_id
        if active_id is None and self._assigned_tasks:
            active_id = next(iter(self._assigned_tasks.keys()))

        return HeartbeatResponse(
            acknowledged=True,
            active_task_id=active_id,
            cancel_task_ids=list(self._cancel_task_ids),
            screenshot_requested=self._screenshot_requested,
        )

    # -------------------------------------------------------------------------
    # Task Polling & Dispatch
    # -------------------------------------------------------------------------

    async def poll_tasks(self, agent_id: str, max_tasks: int = 1) -> PollTasksResponse:
        """Provide queued task assignments to polling Windows Agent."""
        async with self._lock:
            tasks_to_deliver: list[AgentTaskAssignment] = []
            for assignment in list(self._assigned_tasks.values()):
                tasks_to_deliver.append(assignment)
                if len(tasks_to_deliver) >= max_tasks:
                    break

            interval = getattr(
                self.config, "agent_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
            )
            return PollTasksResponse(
                tasks=tasks_to_deliver,
                poll_interval_seconds=interval,
            )

    async def update_task_status(
        self, req: TaskStatusUpdateRequest
    ) -> TaskStatusUpdateResponse:
        """Handle task status progression reported by Windows Agent."""
        cancel_needed = req.task_id in self._cancel_task_ids
        logger.info(
            "Agent %s updated Task #%d status to %s (cancel_requested=%s)",
            req.agent_id,
            req.task_id,
            req.status.value,
            cancel_needed,
        )
        return TaskStatusUpdateResponse(
            acknowledged=True,
            cancel_requested=cancel_needed,
        )

    async def submit_task_result(
        self, req: TaskResultSubmission
    ) -> TaskResultSubmissionResponse:
        """Receive completed task result from Windows Agent and resolve waiting future."""
        logger.info(
            "Received Task #%d result from agent %s (status=%s, duration=%.1fs)",
            req.task_id,
            req.agent_id,
            req.status.value,
            req.duration_seconds,
        )

        res = TaskResult(
            task_id=req.task_id,
            status=req.status,
            exit_code=req.exit_code,
            response=req.response,
            error=req.error,
            duration_seconds=req.duration_seconds,
            soft_denied_tools=req.soft_denied_tools,
            warnings=req.warnings,
            conversation_id=req.conversation_id,
            raw_usage=req.raw_usage,
            completed_at=req.completed_at,
        )

        async with self._lock:
            self._assigned_tasks.pop(req.task_id, None)
            self._cancel_task_ids.discard(req.task_id)

            fut = self._pending_results.pop(req.task_id, None)
            if fut is not None and not fut.done():
                fut.set_result(res)

        return TaskResultSubmissionResponse(acknowledged=True, task_id=req.task_id)

    async def dispatch_and_wait(self, task: TaskRecord) -> TaskResult:
        """Assign task to Windows Agent and await its completion."""
        if not self.is_agent_ready():
            err_msg = (
                "Windows Agent недоступен или Antigravity CLI не найден на целевом компьютере. "
                "Проверьте, включён ли ПК (/wake) и запущен ли agent сервис."
            )
            logger.warning("Cannot dispatch Task #%d: agent is not ready.", task.id)
            return TaskResult(
                task_id=task.id,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=err_msg,
            )

        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[TaskResult] = loop.create_future()

        assignment = AgentTaskAssignment(
            task_id=task.id,
            operation_id=task.operation_id,
            workspace_alias=task.workspace_alias,
            workspace_path=str(task.workspace_path) if task.workspace_path else None,
            prompt=task.prompt,
            effort=task.effort,
            timeout_seconds=self.config.task_execution_timeout_seconds,
            auto_approve_permissions=self.config.cli_auto_approve_permissions,
            conversation_id=task.conversation_id,
        )

        async with self._lock:
            self._assigned_tasks[task.id] = assignment
            self._pending_results[task.id] = result_future

        # Maximum execution window with buffer for network transfer
        timeout = float(self.config.task_execution_timeout_seconds) + 30.0

        try:
            return await asyncio.wait_for(result_future, timeout=timeout)
        except TimeoutError:
            logger.error("Timed out waiting for Task #%d result from agent.", task.id)
            async with self._lock:
                self._assigned_tasks.pop(task.id, None)
                self._pending_results.pop(task.id, None)
            return TaskResult(
                task_id=task.id,
                status=TaskStatus.INTERRUPTED,
                exit_code=124,
                error=f"Таймаут ожидания результата от Windows Agent ({timeout:.0f} сек).",
            )
        except asyncio.CancelledError:
            logger.info("Dispatch of Task #%d was cancelled. Signaling agent.", task.id)
            await self.request_task_cancellation(task.id)
            async with self._lock:
                self._assigned_tasks.pop(task.id, None)
                self._pending_results.pop(task.id, None)
            return TaskResult(
                task_id=task.id,
                status=TaskStatus.CANCELED,
                exit_code=130,
                error="Исполнение задачи было принудительно отменено.",
            )

    async def request_task_cancellation(self, task_id: int) -> bool:
        """Mark task for cancellation so agent will terminate its subprocess."""
        async with self._lock:
            self._cancel_task_ids.add(task_id)
            fut = self._pending_results.get(task_id)
            if fut is not None and not fut.done():
                fut.set_result(
                    TaskResult(
                        task_id=task_id,
                        status=TaskStatus.CANCELED,
                        exit_code=130,
                        error="Задача отменена по запросу пользователя.",
                    )
                )
        return True

    # -------------------------------------------------------------------------
    # Screenshot Request & Receipt
    # -------------------------------------------------------------------------

    async def request_screenshot(self, timeout_seconds: float = 15.0) -> bytes:
        """Signal Windows Agent to capture screenshot and await upload."""
        if not self.is_agent_connected():
            raise RuntimeError(
                "Windows Agent не подключён к Контроллеру. Невозможно сделать снимок экрана."
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()

        async with self._lock:
            self._screenshot_requested = True
            self._screenshot_future = future

        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            raise TimeoutError(
                f"Таймаут ожидания снимка экрана от Windows Agent ({timeout_seconds:.0f} сек)."
            ) from None
        finally:
            async with self._lock:
                self._screenshot_requested = False
                self._screenshot_future = None

    async def submit_screenshot(self, image_bytes: bytes) -> bool:
        """Receive uploaded screenshot from agent and resolve waiting future."""
        async with self._lock:
            self._screenshot_requested = False
            fut = self._screenshot_future
            self._screenshot_future = None

        if fut is not None and not fut.done():
            fut.set_result(image_bytes)
            return True
        return False

    # -------------------------------------------------------------------------
    # Wake-on-LAN Progression
    # -------------------------------------------------------------------------

    async def wait_for_agent_online(self, timeout_seconds: float = 60.0) -> bool:
        """Await agent connection and successful heartbeat registration."""
        if self.is_agent_ready():
            return True

        self._agent_online_event.clear()
        try:
            await asyncio.wait_for(self._agent_online_event.wait(), timeout=timeout_seconds)
            return self.is_agent_ready()
        except TimeoutError:
            return False
