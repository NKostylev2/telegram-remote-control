"""Unit and integration tests for ControllerServer HTTP endpoints and authentication."""

from __future__ import annotations

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from remote_control.common.models import TaskStatus
from remote_control.common.protocol import (
    HeartbeatRequest,
    TaskResultSubmission,
    TaskStatusUpdateRequest,
    build_auth_headers,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.server import create_controller_app


class TestControllerServerEndpoints:
    """Integration tests for Controller REST API endpoints."""

    def test_unauthenticated_request_rejected_with_401(self) -> None:
        async def _test() -> None:
            config = AppConfig(AGENT_AUTH_SECRET="secret_for_testing_purposes_12345")
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/v1/agent/heartbeat", json={"agent_id": "test"})
                assert resp.status == 401
                data = await resp.json()
                assert "Unauthorized" in data["error"]

        asyncio.run(_test())

    def test_bearer_token_authenticated_request_accepted(self) -> None:
        async def _test() -> None:
            secret = "secret_for_testing_purposes_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                req_model = HeartbeatRequest(agent_id="agent-01", hostname="WIN-PC")
                resp = await client.post(
                    "/api/v1/agent/heartbeat",
                    json=req_model.model_dump(mode="json"),
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["acknowledged"] is True

        asyncio.run(_test())

    def test_hmac_signature_authenticated_request_accepted(self) -> None:
        async def _test() -> None:
            secret = "secret_for_testing_purposes_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                req_model = HeartbeatRequest(agent_id="agent-01", hostname="WIN-PC")
                body_bytes = req_model.model_dump_json().encode("utf-8")
                headers = build_auth_headers(
                    agent_id="agent-01",
                    secret=secret,
                    method="POST",
                    path="/api/v1/agent/heartbeat",
                    body=body_bytes,
                )
                headers["Content-Type"] = "application/json"

                resp = await client.post(
                    "/api/v1/agent/heartbeat",
                    data=body_bytes,
                    headers=headers,
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["acknowledged"] is True

        asyncio.run(_test())

    def test_hmac_tampered_request_rejected_with_401(self) -> None:
        async def _test() -> None:
            secret = "secret_for_testing_purposes_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                original_bytes = b'{"agent_id": "legit-agent", "hostname": "PC"}'
                headers = build_auth_headers(
                    agent_id="legit-agent",
                    secret=secret,
                    method="POST",
                    path="/api/v1/agent/heartbeat",
                    body=original_bytes,
                )
                headers["Content-Type"] = "application/json"

                # Attacker changes body
                tampered_bytes = b'{"agent_id": "malicious-agent", "hostname": "PC"}'
                resp = await client.post(
                    "/api/v1/agent/heartbeat",
                    data=tampered_bytes,
                    headers=headers,
                )
                assert resp.status == 401
                data = await resp.json()
                assert "Invalid HMAC signature" in data["error"]

        asyncio.run(_test())

    def test_task_poll_and_result_submission(self) -> None:
        async def _test() -> None:
            secret = "secret_for_testing_purposes_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                auth_hdr = {"Authorization": f"Bearer {secret}"}

                # 1. Poll tasks (empty)
                poll_resp = await client.get("/api/v1/agent/tasks/poll", headers=auth_hdr)
                assert poll_resp.status == 200
                poll_data = await poll_resp.json()
                assert poll_data["tasks"] == []

                # 2. Update status
                status_req = TaskStatusUpdateRequest(
                    agent_id="agent-01",
                    task_id=1,
                    status=TaskStatus.RUNNING,
                )
                st_resp = await client.post(
                    "/api/v1/agent/tasks/1/status",
                    json=status_req.model_dump(mode="json"),
                    headers=auth_hdr,
                )
                assert st_resp.status == 200
                st_data = await st_resp.json()
                assert st_data["acknowledged"] is True

                # 3. Submit result
                result_req = TaskResultSubmission(
                    agent_id="agent-01",
                    task_id=1,
                    status=TaskStatus.SUCCESS,
                    exit_code=0,
                    response="Completed",
                    duration_seconds=1.2,
                )
                res_resp = await client.post(
                    "/api/v1/agent/tasks/1/result",
                    json=result_req.model_dump(mode="json"),
                    headers=auth_hdr,
                )
                assert res_resp.status == 200
                res_data = await res_resp.json()
                assert res_data["acknowledged"] is True
                assert res_data["task_id"] == 1

        asyncio.run(_test())

    def test_screenshot_upload_endpoint(self) -> None:
        async def _test() -> None:
            secret = "secret_for_testing_purposes_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                auth_hdr = {"Authorization": f"Bearer {secret}"}

                fake_screenshot = b"\xff\xd8\xff\xe0some_binary_image_data"
                resp = await client.post(
                    "/api/v1/agent/screenshot",
                    data=fake_screenshot,
                    headers=auth_hdr,
                )
                assert resp.status in (200, 409)

        asyncio.run(_test())

    def test_public_status_endpoint_without_auth(self) -> None:
        async def _test() -> None:
            config = AppConfig(AGENT_AUTH_SECRET="secret_for_testing_purposes_12345")
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/v1/status")
                assert resp.status == 200
                data = await resp.json()
                assert "controller_mode" in data
                assert data["agent_connected"] is False

        asyncio.run(_test())
