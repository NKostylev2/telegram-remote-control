"""HTTP client for Windows Agent communicating with Android Controller.

Handles HMAC-SHA256 signature generation, replay protection headers,
network timeouts, retries, and error handling for:
- Heartbeat registration
- Task polling
- Task status reporting
- Task result submission
- Screenshot uploading
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, unquote

import aiohttp

from remote_control.common.protocol import (
    DirectoryBrowseResponse,
    FileUploadAck,
    HeartbeatRequest,
    HeartbeatResponse,
    PollTasksResponse,
    TaskResultSubmission,
    TaskResultSubmissionResponse,
    TaskStatusUpdateRequest,
    TaskStatusUpdateResponse,
    build_auth_headers,
)


logger = logging.getLogger(__name__)


class AgentHttpClientError(Exception):
    """Base exception for Agent HTTP client errors."""


class AgentUnauthorizedError(AgentHttpClientError):
    """Raised when Controller returns HTTP 401 Unauthorized."""


class AgentHttpClient:
    """Async HTTP client connecting Windows Agent to Android Controller API."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        secret: str | None = None,
        session: aiohttp.ClientSession | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.secret = secret
        self._external_session = session is not None
        self._session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        """Close client session if internally created."""
        if not self._external_session and self._session is not None and not self._session.closed:
            await self._session.close()

    def _prepare_headers(
        self,
        method: str,
        path: str,
        body: bytes = b"",
    ) -> dict[str, str]:
        """Construct request headers with authentication."""
        if not self.secret:
            return {
                "X-Agent-ID": self.agent_id,
            }
        return build_auth_headers(
            agent_id=self.agent_id,
            secret=self.secret,
            method=method,
            path=path,
            body=body,
        )

    async def send_heartbeat(self, req: HeartbeatRequest) -> HeartbeatResponse:
        """Transmit heartbeat to Controller."""
        session = await self._get_session()
        path = "/api/v1/agent/heartbeat"
        body_bytes = req.model_dump_json().encode("utf-8")
        headers = self._prepare_headers("POST", path, body_bytes)
        headers["Content-Type"] = "application/json"

        async with session.post(f"{self.base_url}{path}", data=body_bytes, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            resp.raise_for_status()
            data = await resp.json()
            return HeartbeatResponse.model_validate(data)

    async def poll_tasks(self, max_tasks: int = 1) -> PollTasksResponse:
        """Poll Controller for newly assigned tasks."""
        session = await self._get_session()
        path = "/api/v1/agent/tasks/poll"
        headers = self._prepare_headers("GET", path)

        params = {"agent_id": self.agent_id, "max_tasks": str(max_tasks)}
        async with session.get(f"{self.base_url}{path}", params=params, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            resp.raise_for_status()
            data = await resp.json()
            return PollTasksResponse.model_validate(data)

    async def update_task_status(
        self, req: TaskStatusUpdateRequest
    ) -> TaskStatusUpdateResponse:
        """Notify Controller of task execution status change."""
        session = await self._get_session()
        path = f"/api/v1/agent/tasks/{req.task_id}/status"
        body_bytes = req.model_dump_json().encode("utf-8")
        headers = self._prepare_headers("POST", path, body_bytes)
        headers["Content-Type"] = "application/json"

        async with session.post(f"{self.base_url}{path}", data=body_bytes, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            resp.raise_for_status()
            data = await resp.json()
            return TaskStatusUpdateResponse.model_validate(data)

    async def submit_task_result(
        self, req: TaskResultSubmission
    ) -> TaskResultSubmissionResponse:
        """Submit completed task results to Controller."""
        session = await self._get_session()
        path = f"/api/v1/agent/tasks/{req.task_id}/result"
        body_bytes = req.model_dump_json().encode("utf-8")
        headers = self._prepare_headers("POST", path, body_bytes)
        headers["Content-Type"] = "application/json"

        async with session.post(f"{self.base_url}{path}", data=body_bytes, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            resp.raise_for_status()
            data = await resp.json()
            return TaskResultSubmissionResponse.model_validate(data)

    async def upload_screenshot(self, image_bytes: bytes) -> bool:
        """Upload raw desktop screenshot JPEG bytes to Controller."""
        session = await self._get_session()
        path = "/api/v1/agent/screenshot"
        headers = self._prepare_headers("POST", path, image_bytes)
        headers["Content-Type"] = "image/jpeg"
        url = f"{self.base_url}{path}"
        async with session.post(url, data=image_bytes, headers=headers) as resp:
            return resp.status == 200

    async def submit_browse_response(self, res: DirectoryBrowseResponse) -> bool:
        """Submit directory browse results to Controller."""
        session = await self._get_session()
        path = "/api/v1/agent/browse"
        body_bytes = res.model_dump_json().encode("utf-8")
        headers = self._prepare_headers("POST", path, body_bytes)
        headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        async with session.post(url, data=body_bytes, headers=headers) as resp:
            return resp.status == 200

    async def upload_downloaded_file(
        self,
        request_id: str,
        filename: str,
        data: bytes,
        error: str | None = None,
    ) -> bool:
        """Upload a file downloaded from Windows PC to the Controller."""
        session = await self._get_session()
        path = "/api/v1/agent/files/download"
        headers = self._prepare_headers("POST", path, data)
        headers["X-Request-ID"] = request_id
        headers["X-File-Name"] = quote(filename)
        if error:
            headers["X-File-Error"] = quote(error)
        headers["Content-Type"] = "application/octet-stream"

        url = f"{self.base_url}{path}"
        async with session.post(url, data=data, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            return resp.status == 200

    async def fetch_staged_upload(
        self, transfer_id: str
    ) -> tuple[dict[str, str], bytes]:
        """Fetch a file staged on Controller for writing to local Windows disk."""
        session = await self._get_session()
        path = f"/api/v1/agent/files/upload/{transfer_id}"
        headers = self._prepare_headers("GET", path)

        url = f"{self.base_url}{path}"
        async with session.get(url, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            resp.raise_for_status()
            target_dir = unquote(resp.headers.get("X-Target-Dir", ""))
            filename = unquote(resp.headers.get("X-Target-Filename", ""))
            conflict_mode = resp.headers.get("X-Conflict-Mode", "overwrite")
            data = await resp.read()
            metadata = {
                "transfer_id": transfer_id,
                "target_dir": target_dir,
                "filename": filename,
                "conflict_mode": conflict_mode,
            }
            return metadata, data

    async def acknowledge_upload(self, ack: FileUploadAck) -> bool:
        """Send confirmation to Controller after saving uploaded file to local disk."""
        session = await self._get_session()
        path = f"/api/v1/agent/files/upload/{ack.transfer_id}/ack"
        body_bytes = ack.model_dump_json().encode("utf-8")
        headers = self._prepare_headers("POST", path, body_bytes)
        headers["Content-Type"] = "application/json"

        url = f"{self.base_url}{path}"
        async with session.post(url, data=body_bytes, headers=headers) as resp:
            if resp.status == 401:
                raise AgentUnauthorizedError("Controller rejected authentication credentials (401)")
            return resp.status == 200

    async def get_controller_status(self) -> dict[str, Any]:
        """Fetch general status and diagnostics from Controller."""
        session = await self._get_session()
        path = "/api/v1/status"
        async with session.get(f"{self.base_url}{path}") as resp:
            resp.raise_for_status()
            return await resp.json()

