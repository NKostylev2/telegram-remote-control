"""Task execution abstraction layer supporting local and distributed modes.

Provides a unified interface (TaskExecutor) for executing Antigravity CLI tasks:
- LocalTaskExecutor: Direct local execution via AgyClient (Windows monolithic mode).
- RemoteTaskExecutor: Dispatches tasks to Windows Agent via AgentDispatcher.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import TYPE_CHECKING, Any

from remote_control.agent.agy_client import AgyClient
from remote_control.common.models import TaskResult, TaskStatus

if TYPE_CHECKING:
    from remote_control.controller.task_store import TaskRecord

logger = logging.getLogger(__name__)


class TaskExecutor(abc.ABC):
    """Abstract base class for executing tasks either locally or via a remote agent."""

    @abc.abstractmethod
    async def execute_task(self, task: TaskRecord) -> TaskResult:
        """Execute the task and return the structured result."""

    @abc.abstractmethod
    async def cancel_task(self, task: TaskRecord) -> bool:
        """Attempt to cancel an actively executing task."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True if executor is ready and able to accept tasks."""


class LocalTaskExecutor(TaskExecutor):
    """Executes tasks on the local system directly invoking AgyClient."""

    def __init__(self, agy_client: AgyClient) -> None:
        self.agy_client = agy_client
        self._current_task: asyncio.Task[TaskResult] | None = None
        self._lock = asyncio.Lock()

    def is_available(self) -> bool:
        """Check if Antigravity CLI is installed and available locally."""
        return self.agy_client.is_cli_available()

    async def execute_task(self, task: TaskRecord) -> TaskResult:
        """Run task locally using AgyClient subprocess."""
        async with self._lock:
            self._current_task = asyncio.create_task(
                self.agy_client.execute(
                    prompt=task.prompt,
                    workspace_alias=task.workspace_alias if not task.workspace_path else None,
                    workspace_path=task.workspace_path,
                    effort=task.effort,
                )
            )

        try:
            return await self._current_task
        except asyncio.CancelledError:
            logger.info("Local execution of Task #%d was cancelled.", task.id)
            return TaskResult(
                task_id=task.operation_id,
                status=TaskStatus.CANCELED,
                exit_code=130,
                error="Исполнение задачи было принудительно отменено.",
            )
        except Exception as e:
            logger.exception("Local execution error for Task #%d: %s", task.id, e)
            return TaskResult(
                task_id=task.operation_id,
                status=TaskStatus.ERROR,
                exit_code=1,
                error=f"Внутренняя ошибка выполнения CLI: {e}",
            )
        finally:
            self._current_task = None

    async def cancel_task(self, task: TaskRecord) -> bool:
        """Cancel the locally running CLI subprocess."""
        if self._current_task is not None and not self._current_task.done():
            self._current_task.cancel()
            return True
        return False


class RemoteTaskExecutor(TaskExecutor):
    """Dispatches tasks to the connected Windows Agent via an AgentDispatcher."""

    def __init__(self, dispatcher: Any) -> None:
        self.dispatcher = dispatcher

    def is_available(self) -> bool:
        """Check if a Windows Agent is currently connected and reports CLI available."""
        return self.dispatcher.is_agent_ready()

    async def execute_task(self, task: TaskRecord) -> TaskResult:
        """Dispatch task to Windows Agent and await its completion."""
        return await self.dispatcher.dispatch_and_wait(task)

    async def cancel_task(self, task: TaskRecord) -> bool:
        """Request cancellation of task running on the remote Windows Agent."""
        return await self.dispatcher.request_task_cancellation(task.id)
