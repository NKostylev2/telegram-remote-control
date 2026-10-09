"""In-memory confirmation store for Telegram task execution requests.

Provides temporary, non-persistent storage for pending /run actions requiring
explicit human confirmation via Telegram inline buttons.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class ConfirmationStatus(StrEnum):
    """Lifecycle state of a confirmation operation."""

    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class PendingConfirmation:
    """Represents a single pending task confirmation."""

    operation_id: str
    user_id: int
    chat_id: int
    workspace_alias: str
    prompt: str
    created_at: float
    expires_at: float
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    workspace_path: Path | None = None
    interpretation: str | None = None
    effort: str = "low"

    @property
    def is_expired(self) -> bool:
        """Check whether the operation has exceeded its expiration timestamp."""
        return time.time() > self.expires_at


class ConfirmationStore:
    """Thread-safe and async-friendly in-memory storage of pending confirmations."""

    def __init__(self, default_ttl_seconds: float = 120.0) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._operations: dict[str, PendingConfirmation] = {}

    def create_operation(
        self,
        user_id: int,
        chat_id: int,
        workspace_alias: str,
        prompt: str,
        ttl_seconds: float | None = None,
        workspace_path: Path | None = None,
        interpretation: str | None = None,
        effort: str = "low",
    ) -> PendingConfirmation:
        """Create and register a new pending confirmation operation."""
        self.cleanup_expired()

        ttl = (
            ttl_seconds
            if ttl_seconds is not None and ttl_seconds > 0
            else self.default_ttl_seconds
        )
        now = time.time()
        op_id = uuid4().hex[:12]

        op = PendingConfirmation(
            operation_id=op_id,
            user_id=user_id,
            chat_id=chat_id,
            workspace_alias=workspace_alias,
            prompt=prompt,
            created_at=now,
            expires_at=now + ttl,
            status=ConfirmationStatus.PENDING,
            workspace_path=workspace_path,
            interpretation=interpretation,
            effort=effort,
        )
        self._operations[op_id] = op
        return op

    def set_effort(self, operation_id: str, effort: str) -> bool:
        """Update the reasoning effort for an active pending operation."""
        op = self.get_operation(operation_id)
        if op and op.status == ConfirmationStatus.PENDING:
            op.effort = effort
            return True
        return False

    def get_operation(self, operation_id: str) -> PendingConfirmation | None:
        """Retrieve an operation by ID if it exists."""
        return self._operations.get(operation_id)

    def confirm_operation(
        self,
        operation_id: str,
        user_id: int,
        chat_id: int,
    ) -> tuple[bool, str, PendingConfirmation | None]:
        """Validate and claim a confirmation operation for execution.

        Returns:
            (success, error_message, operation)
        """
        op = self._operations.get(operation_id)
        if op is None:
            return False, "Операция подтверждения не найдена или уже удалена.", None

        if op.user_id != user_id:
            return False, "Вы не являетесь инициатором этой задачи.", None

        if op.chat_id != chat_id:
            return False, "Операция должна подтверждаться в том же чате.", None

        if op.is_expired:
            op.status = ConfirmationStatus.EXPIRED
            return False, "Срок действия подтверждения истёк (120 секунд).", None

        if op.status != ConfirmationStatus.PENDING:
            return False, f"Операция уже обработана (статус: {op.status}).", None

        # Atomically mark as EXECUTING to prevent race condition / duplicate triggers
        op.status = ConfirmationStatus.EXECUTING
        return True, "", op

    def cancel_operation(
        self,
        operation_id: str,
        user_id: int,
        chat_id: int,
    ) -> tuple[bool, str, PendingConfirmation | None]:
        """Validate and cancel a pending confirmation operation.

        Returns:
            (success, error_message, operation)
        """
        op = self._operations.get(operation_id)
        if op is None:
            return False, "Операция не найдена или уже удалена.", None

        if op.user_id != user_id:
            return False, "Вы не являетесь инициатором этой задачи.", None

        if op.chat_id != chat_id:
            return False, "Операция должна быть отменена в том же чате.", None

        if op.is_expired:
            op.status = ConfirmationStatus.EXPIRED
            return False, "Срок действия операции уже истёк.", None

        if op.status != ConfirmationStatus.PENDING:
            return False, f"Операция уже обработана (статус: {op.status}).", None

        op.status = ConfirmationStatus.CANCELLED
        return True, "", op

    def mark_completed(self, operation_id: str) -> None:
        """Mark an active operation as completed."""
        op = self._operations.get(operation_id)
        if op is not None:
            op.status = ConfirmationStatus.COMPLETED

    def cleanup_expired(self, max_retention_seconds: float = 3600.0) -> int:
        """Purge old operations to prevent unbounded in-memory growth."""
        now = time.time()
        to_delete: list[str] = []
        for op_id, op in self._operations.items():
            if now - op.expires_at > max_retention_seconds:
                to_delete.append(op_id)

        for op_id in to_delete:
            self._operations.pop(op_id, None)

        return len(to_delete)
