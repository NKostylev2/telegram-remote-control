"""Asynchronous HTTP API server for Controller <-> Agent communication.

Provides secure REST endpoints for the Windows Agent using aiohttp.web:
- Mutual authentication via HMAC-SHA256 signature or Bearer secret.
- Replay attack mitigation with timestamp validation.
- Heartbeat registration and capability discovery.
- Fast polling of assigned tasks (target latency 250-500 ms).
- Task status reporting and result ingestion.
- Binary JPEG desktop screenshot upload.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from urllib.parse import quote, unquote

from remote_control.common.protocol import (
    MAX_PAYLOAD_BYTES,
    DirectoryBrowseResponse,
    FileUploadAck,
    HeartbeatRequest,
    TaskResultSubmission,
    TaskStatusUpdateRequest,
    verify_signature,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher


logger = logging.getLogger(__name__)


def create_auth_middleware(config: AppConfig) -> web.middleware:
    """Create aiohttp middleware verifying HMAC-SHA256 signatures or Bearer tokens."""
    @web.middleware
    async def auth_middleware(
        request: web.Request,
        handler: Any,
    ) -> web.StreamResponse:
        # Public healthcheck and wake status endpoints do not require credentials
        if request.path in ("/api/v1/status", "/api/v1/wake/status"):
            return await handler(request)

        secret_wrapper = config.agent_auth_secret
        if secret_wrapper is None:
            # If no secret configured, allow (e.g. dev mode)
            return await handler(request)

        secret = secret_wrapper.get_secret_value()

        # 1. Check Bearer token header
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, secret):
                return await handler(request)

        # 2. Check HMAC-SHA256 cryptographic signature
        sig = request.headers.get("X-Signature", "").strip()
        ts = request.headers.get("X-Timestamp", "").strip()

        if sig and ts:
            body = await request.read()
            is_valid, err = verify_signature(
                secret=secret,
                signature=sig,
                timestamp_str=ts,
                method=request.method,
                path=request.path,
                body=body,
            )
            if is_valid:
                return await handler(request)
            logger.warning("HMAC signature verification failed from %s: %s", request.remote, err)
            return web.json_response(
                {"error": f"Unauthorized: {err}"},
                status=401,
            )

        logger.warning("Unauthorized request from %s: missing credentials", request.remote)
        return web.json_response(
            {"error": "Unauthorized: Missing authentication credentials"},
            status=401,
        )

    return auth_middleware


def create_controller_app(
    config: AppConfig,
    dispatcher: AgentDispatcher,
) -> web.Application:
    """Configure and build aiohttp application for the Controller."""
    app = web.Application(
        client_max_size=MAX_PAYLOAD_BYTES,
        middlewares=[create_auth_middleware(config)],
    )

    # -------------------------------------------------------------------------
    # Route Handlers
    # -------------------------------------------------------------------------

    async def handle_heartbeat(request: web.Request) -> web.Response:
        try:
            raw_data = await request.json()
            req = HeartbeatRequest.model_validate(raw_data)
        except Exception as e:
            return web.json_response({"error": f"Invalid heartbeat payload: {e}"}, status=400)

        resp = await dispatcher.handle_heartbeat(req)
        return web.json_response(resp.model_dump(mode="json"))

    async def handle_poll_tasks(request: web.Request) -> web.Response:
        agent_id = request.headers.get("X-Agent-ID", request.query.get("agent_id", "default"))
        max_tasks = int(request.query.get("max_tasks", "1"))
        resp = await dispatcher.poll_tasks(agent_id=agent_id, max_tasks=max_tasks)
        return web.json_response(resp.model_dump(mode="json"))

    async def handle_task_status(request: web.Request) -> web.Response:
        try:
            raw_data = await request.json()
            req = TaskStatusUpdateRequest.model_validate(raw_data)
        except Exception as e:
            return web.json_response({"error": f"Invalid task status payload: {e}"}, status=400)

        resp = await dispatcher.update_task_status(req)
        return web.json_response(resp.model_dump(mode="json"))

    async def handle_task_result(request: web.Request) -> web.Response:
        try:
            raw_data = await request.json()
            req = TaskResultSubmission.model_validate(raw_data)
        except Exception as e:
            return web.json_response({"error": f"Invalid task result payload: {e}"}, status=400)

        resp = await dispatcher.submit_task_result(req)
        return web.json_response(resp.model_dump(mode="json"))

    async def handle_screenshot(request: web.Request) -> web.Response:
        body = await request.read()
        if not body:
            return web.json_response({"error": "Empty screenshot payload"}, status=400)

        success = await dispatcher.submit_screenshot(body)
        return web.json_response(
            {"acknowledged": success, "received_bytes": len(body)},
            status=200 if success else 409,
        )

    async def handle_browse(request: web.Request) -> web.Response:
        try:
            raw_data = await request.json()
            req = DirectoryBrowseResponse.model_validate(raw_data)
        except Exception as e:
            return web.json_response({"error": f"Invalid browse payload: {e}"}, status=400)

        success = await dispatcher.submit_browse_response(req)
        return web.json_response({"acknowledged": success})

    async def handle_status(request: web.Request) -> web.Response:
        agent_info = dispatcher.get_agent_info()
        return web.json_response(
            {
                "controller_mode": config.controller_mode,
                "agent_connected": dispatcher.is_agent_connected(),
                "agent_ready": dispatcher.is_agent_ready(),
                "agent_info": agent_info.model_dump(mode="json") if agent_info else None,
            }
        )

    async def handle_wake_status(request: web.Request) -> web.Response:
        consume_param = request.query.get("consume", "true").lower() in ("true", "1", "yes")
        try:
            window_param = float(request.query.get("window", "300.0"))
        except ValueError:
            window_param = 300.0

        was_woken = dispatcher.check_and_consume_wake_request(
            window_seconds=window_param,
            consume=consume_param,
        )
        return web.json_response(
            {
                "wake_requested": was_woken,
                "consumed": consume_param if was_woken else False,
            }
        )

    async def handle_file_download(request: web.Request) -> web.Response:
        req_id = request.headers.get("X-Request-ID") or request.query.get("request_id", "")
        raw_filename = request.headers.get("X-File-Name", "")
        filename = unquote(raw_filename) if raw_filename else "downloaded_file"
        file_err = request.headers.get("X-File-Error")
        if file_err:
            file_err = unquote(file_err)

        if not req_id:
            return web.json_response({"error": "Missing X-Request-ID header"}, status=400)

        if file_err:
            await dispatcher.submit_file_download(
                request_id=req_id,
                filename="",
                data=b"",
                error=file_err,
            )
            return web.json_response({"acknowledged": True, "error_recorded": True})

        data = await request.read()
        success = await dispatcher.submit_file_download(
            request_id=req_id,
            filename=filename,
            data=data,
        )
        return web.json_response(
            {"acknowledged": success, "received_bytes": len(data)},
            status=200 if success else 409,
        )

    async def handle_file_upload_get(request: web.Request) -> web.Response:
        transfer_id = request.match_info.get("transfer_id", "")
        staged = dispatcher.get_staged_upload(transfer_id)
        if staged is None:
            return web.json_response({"error": "Upload instruction not found or expired"}, status=404)

        instruction, payload = staged
        return web.Response(
            body=payload,
            content_type="application/octet-stream",
            headers={
                "X-Transfer-ID": instruction.transfer_id,
                "X-Target-Dir": quote(instruction.target_dir),
                "X-Target-Filename": quote(instruction.filename),
                "X-Conflict-Mode": instruction.conflict_mode,
                "X-File-Size": str(len(payload)),
            },
        )

    async def handle_file_upload_ack(request: web.Request) -> web.Response:
        transfer_id = request.match_info.get("transfer_id", "")
        try:
            raw_data = await request.json()
            ack = FileUploadAck.model_validate(raw_data)
        except Exception as e:
            return web.json_response({"error": f"Invalid upload ack payload: {e}"}, status=400)

        success = await dispatcher.submit_upload_ack(ack)
        return web.json_response({"acknowledged": success})

    # -------------------------------------------------------------------------
    # Route Registration
    # -------------------------------------------------------------------------

    app.router.add_post("/api/v1/agent/heartbeat", handle_heartbeat)
    app.router.add_get("/api/v1/agent/tasks/poll", handle_poll_tasks)
    app.router.add_post("/api/v1/agent/tasks/{task_id}/status", handle_task_status)
    app.router.add_post("/api/v1/agent/tasks/{task_id}/result", handle_task_result)
    app.router.add_post("/api/v1/agent/screenshot", handle_screenshot)
    app.router.add_post("/api/v1/agent/browse", handle_browse)
    app.router.add_post("/api/v1/agent/files/download", handle_file_download)
    app.router.add_get("/api/v1/agent/files/upload/{transfer_id}", handle_file_upload_get)
    app.router.add_post("/api/v1/agent/files/upload/{transfer_id}/ack", handle_file_upload_ack)
    app.router.add_get("/api/v1/status", handle_status)
    app.router.add_get("/api/v1/wake/status", handle_wake_status)

    return app



class ControllerServer:
    """Manages lifecycle of the Controller HTTP server."""

    def __init__(self, config: AppConfig, dispatcher: AgentDispatcher) -> None:
        self.config = config
        self.dispatcher = dispatcher
        self.app = create_controller_app(config, dispatcher)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start the background HTTP listening socket."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            host=self.config.controller_host,
            port=self.config.controller_port,
        )
        await self.site.start()
        logger.info(
            "Controller HTTP API listening at http://%s:%d",
            self.config.controller_host,
            self.config.controller_port,
        )

    async def stop(self) -> None:
        """Gracefully terminate HTTP server."""
        if self.runner is not None:
            logger.info("Stopping Controller HTTP API...")
            await self.runner.cleanup()
            self.runner = None
            self.site = None
