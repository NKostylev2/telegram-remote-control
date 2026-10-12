"""Unit tests for Controller <-> Agent protocol v2 models and HMAC-SHA256 authentication."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

from remote_control.common.models import TaskStatus
from remote_control.common.protocol import (
    PROTOCOL_VERSION,
    AgentStatusInfo,
    AgentTaskAssignment,
    HeartbeatRequest,
    HeartbeatResponse,
    PollTasksRequest,
    PollTasksResponse,
    ScreenshotMetadata,
    TaskResultSubmission,
    TaskResultSubmissionResponse,
    TaskStatusUpdateRequest,
    TaskStatusUpdateResponse,
    build_auth_headers,
    compute_signature,
    verify_signature,
)


class TestProtocolAuthentication:
    """Tests for HMAC-SHA256 request signing and replay prevention."""

    def test_compute_and_verify_signature_success(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        timestamp = str(time.time())
        method = "POST"
        path = "/api/v1/agent/heartbeat"
        body = b'{"agent_id": "win-1"}'

        sig = compute_signature(secret, timestamp, method, path, body)
        valid, err = verify_signature(secret, sig, timestamp, method, path, body)

        assert valid is True
        assert err == ""

    def test_verify_signature_tampered_body(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        timestamp = str(time.time())
        method = "POST"
        path = "/api/v1/agent/heartbeat"
        body = b'{"agent_id": "win-1"}'

        sig = compute_signature(secret, timestamp, method, path, body)
        tampered_body = b'{"agent_id": "attacker-agent"}'
        valid, err = verify_signature(secret, sig, timestamp, method, path, tampered_body)

        assert valid is False
        assert "Invalid HMAC signature" in err

    def test_verify_signature_tampered_path(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        timestamp = str(time.time())
        body = b'{"key": "val"}'

        sig = compute_signature(secret, timestamp, "GET", "/api/v1/agent/tasks/poll", body)
        valid, err = verify_signature(
            secret, sig, timestamp, "GET", "/api/v1/agent/admin/danger", body
        )

        assert valid is False
        assert "Invalid HMAC signature" in err

    def test_verify_signature_wrong_secret(self) -> None:
        secret1 = "super_secure_random_test_secret_key_12345"
        secret2 = "different_secret_key_9876543210000000"
        timestamp = str(time.time())

        sig = compute_signature(secret1, timestamp, "POST", "/api/v1/agent/tasks/1/result")
        valid, err = verify_signature(
            secret2, sig, timestamp, "POST", "/api/v1/agent/tasks/1/result"
        )

        assert valid is False
        assert "Invalid HMAC signature" in err

    def test_replay_attack_rejected_on_stale_timestamp(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        # 120 seconds in the past (exceeds default MAX_TIMESTAMP_DRIFT_SECONDS = 60s)
        stale_timestamp = str(time.time() - 120.0)

        sig = compute_signature(secret, stale_timestamp, "POST", "/api/v1/agent/heartbeat")
        valid, err = verify_signature(
            secret, sig, stale_timestamp, "POST", "/api/v1/agent/heartbeat"
        )

        assert valid is False
        assert "Timestamp drift exceeds limit" in err

    def test_replay_attack_rejected_on_future_timestamp(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        # 120 seconds in the future
        future_timestamp = str(time.time() + 120.0)

        sig = compute_signature(secret, future_timestamp, "POST", "/api/v1/agent/heartbeat")
        valid, err = verify_signature(
            secret, sig, future_timestamp, "POST", "/api/v1/agent/heartbeat"
        )

        assert valid is False
        assert "Timestamp drift exceeds limit" in err

    def test_verify_signature_missing_headers(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        valid, err = verify_signature(secret, "", str(time.time()), "GET", "/path")
        assert valid is False
        assert "Missing X-Signature" in err

        valid, err = verify_signature(secret, "some_sig", "", "GET", "/path")
        assert valid is False
        assert "Missing X-Timestamp" in err

        valid, err = verify_signature(secret, "some_sig", "invalid_number", "GET", "/path")
        assert valid is False
        assert "Invalid timestamp format" in err

    def test_build_auth_headers_contains_required_fields(self) -> None:
        secret = "super_secure_random_test_secret_key_12345"
        headers = build_auth_headers(
            agent_id="test-agent-win11",
            secret=secret,
            method="POST",
            path="/api/v1/agent/heartbeat",
            body=b"payload",
        )

        assert headers["X-Agent-ID"] == "test-agent-win11"
        assert headers["X-Protocol-Version"] == PROTOCOL_VERSION
        assert "X-Timestamp" in headers
        assert "X-Signature" in headers

        # Signature must verify against headers
        valid, err = verify_signature(
            secret=secret,
            signature=headers["X-Signature"],
            timestamp_str=headers["X-Timestamp"],
            method="POST",
            path="/api/v1/agent/heartbeat",
            body=b"payload",
        )
        assert valid is True
        assert err == ""


class TestProtocolModels:
    """Tests for protocol request/response model validation and serialization."""

    def test_heartbeat_request_and_response(self) -> None:
        req = HeartbeatRequest(
            agent_id="win-agent-01",
            hostname="DESKTOP-TEST",
            cli_available=True,
            cli_version="1.3.2",
            available_workspaces={"backend": "C:\\dev\\backend"},
            current_task_id=None,
        )
        assert req.protocol_version == PROTOCOL_VERSION
        assert req.cli_available is True
        data = req.model_dump()
        assert data["agent_id"] == "win-agent-01"

        resp = HeartbeatResponse(
            acknowledged=True,
            cancel_task_ids=[42],
            screenshot_requested=False,
        )
        assert resp.acknowledged is True
        assert resp.cancel_task_ids == [42]

    def test_poll_tasks_request_and_response(self) -> None:
        poll_req = PollTasksRequest(agent_id="win-agent-01")
        assert poll_req.protocol_version == PROTOCOL_VERSION
        assert len(poll_req.request_id) > 10

        assignment = AgentTaskAssignment(
            task_id=1,
            operation_id=uuid4().hex[:12],
            workspace_alias="backend",
            prompt="git status",
            effort="low",
            timeout_seconds=60,
        )
        poll_resp = PollTasksResponse(tasks=[assignment], poll_interval_seconds=0.5)
        assert len(poll_resp.tasks) == 1
        assert poll_resp.tasks[0].task_id == 1
        assert poll_resp.tasks[0].prompt == "git status"

    def test_task_status_update(self) -> None:
        req = TaskStatusUpdateRequest(
            agent_id="win-agent-01",
            task_id=10,
            status=TaskStatus.RUNNING,
            message="CLI process started",
        )
        assert req.status == TaskStatus.RUNNING
        resp = TaskStatusUpdateResponse(acknowledged=True, cancel_requested=False)
        assert resp.acknowledged is True

    def test_task_result_submission(self) -> None:
        result = TaskResultSubmission(
            agent_id="win-agent-01",
            task_id=10,
            status=TaskStatus.SUCCESS,
            exit_code=0,
            response="Task output completed",
            duration_seconds=5.2,
            soft_denied_tools=[],
            warnings=[],
            conversation_id="conv-123",
        )
        assert result.status == TaskStatus.SUCCESS
        assert result.exit_code == 0
        assert result.conversation_id == "conv-123"

        resp = TaskResultSubmissionResponse(acknowledged=True, task_id=10)
        assert resp.acknowledged is True
        assert resp.task_id == 10

    def test_screenshot_metadata(self) -> None:
        meta = ScreenshotMetadata(
            agent_id="win-agent-01",
            width=1920,
            height=1080,
            format="jpeg",
        )
        assert meta.width == 1920
        assert meta.height == 1080
        assert meta.format == "jpeg"
        assert meta.error is None

    def test_agent_status_info(self) -> None:
        info = AgentStatusInfo(
            agent_id="win-agent-01",
            hostname="MY-PC",
            platform="windows",
            cli_available=True,
            cli_version="1.3.2",
            available_workspaces={"work": "D:\\Work"},
            last_heartbeat=datetime.now(UTC),
            is_online=True,
        )
        assert info.is_online is True
        assert info.hostname == "MY-PC"
