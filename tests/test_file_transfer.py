"""Comprehensive unit and integration tests for bidirectional file transfer and browsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote, unquote

import pytest
from aiohttp.test_utils import TestClient, TestServer

from remote_control.agent.client import AgentHttpClient
from remote_control.common.protocol import (
    MAX_FILE_DOWNLOAD_BYTES,
    MAX_FILE_UPLOAD_BYTES,
    DirectoryBrowseResponse,
    FileItem,
    FileUploadAck,
    FileUploadInstruction,
    HeartbeatRequest,
    build_auth_headers,
)
from remote_control.config import AppConfig
from remote_control.controller.agent_dispatcher import AgentDispatcher
from remote_control.controller.browser import (
    format_file_size,
    list_directory_contents,
    list_subdirectories,
)
from remote_control.controller.server import create_controller_app


class TestBrowserUtils:
    """Tests for browser utilities and file listing."""

    def test_format_file_size(self) -> None:
        assert format_file_size(500) == "500 B"
        assert format_file_size(1024) == "1.0 KB"
        assert format_file_size(1536) == "1.5 KB"
        assert format_file_size(10 * 1024 * 1024) == "10.0 MB"
        assert format_file_size(2 * 1024 * 1024 * 1024) == "2.0 GB"

    def test_list_directory_contents(self, tmp_path: Path) -> None:
        # Create test folder structure
        d1 = tmp_path / "subdir_b"
        d2 = tmp_path / "subdir_a"
        d1.mkdir()
        d2.mkdir()

        # Hidden folder
        (tmp_path / ".hidden_dir").mkdir()

        # Files
        f1 = tmp_path / "file_2.txt"
        f2 = tmp_path / "file_1.log"
        f1.write_text("hello", encoding="utf-8")
        f2.write_text("world test", encoding="utf-8")

        # Hidden file
        (tmp_path / ".hidden_file").write_text("secret", encoding="utf-8")

        subdirs, files = list_directory_contents(tmp_path)

        # Folders should be sorted alphabetically, hidden excluded
        assert [p.name for p in subdirs] == ["subdir_a", "subdir_b"]

        # Files should be sorted alphabetically, hidden excluded
        assert [f.name for f in files] == ["file_1.log", "file_2.txt"]
        assert files[0].size_bytes == len("world test")
        assert files[1].size_bytes == len("hello")

        # Backwards compatible list_subdirectories
        subdirs_only = list_subdirectories(tmp_path)
        assert [p.name for p in subdirs_only] == ["subdir_a", "subdir_b"]


class TestProtocolModels:
    """Tests for file transfer protocol models."""

    def test_file_item_serialization(self) -> None:
        item = FileItem(name="report.pdf", size_bytes=10240, modified_at=1700000000.0)
        data = item.model_dump(mode="json")
        restored = FileItem.model_validate(data)
        assert restored.name == "report.pdf"
        assert restored.size_bytes == 10240
        assert restored.modified_at == 1700000000.0

    def test_directory_browse_response_with_files(self) -> None:
        files = [
            FileItem(name="a.txt", size_bytes=10),
            FileItem(name="b.pdf", size_bytes=200),
        ]
        resp = DirectoryBrowseResponse(
            request_id="req-123",
            path="C:\\test",
            subdirectories=["C:\\test\\sub1"],
            files=files,
        )
        data = resp.model_dump(mode="json")
        restored = DirectoryBrowseResponse.model_validate(data)
        assert restored.request_id == "req-123"
        assert len(restored.files) == 2
        assert restored.files[1].name == "b.pdf"

    def test_file_upload_instruction_and_ack(self) -> None:
        instr = FileUploadInstruction(
            transfer_id="tr-456",
            target_dir="C:\\dev",
            filename="code.py",
            conflict_mode="suffix",
            file_size=512,
        )
        assert instr.conflict_mode == "suffix"
        assert instr.file_size == 512

        ack = FileUploadAck(
            transfer_id="tr-456",
            success=True,
            saved_path="C:\\dev\\code (1).py",
            bytes_written=512,
        )
        assert ack.success is True
        assert ack.saved_path == "C:\\dev\\code (1).py"


class TestAgentDispatcherFileTransfer:
    """Tests for AgentDispatcher file download and upload workflows."""

    def test_file_download_flow(self) -> None:
        async def _test() -> None:
            config = AppConfig(AGENT_AUTH_SECRET="super_secret_test_key_12345")
            dispatcher = AgentDispatcher(config)

            # Simulate agent online
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="test-agent", hostname="WIN-PC")
            )

            async def _client_sim():
                await asyncio.sleep(0.05)
                # Agent polls and finds download request
                poll_resp = await dispatcher.poll_tasks(agent_id="test-agent")
                assert poll_resp.file_download_request_id is not None
                assert poll_resp.file_download_path == "C:\\docs\\report.pdf"

                # Agent submits downloaded file bytes
                await dispatcher.submit_file_download(
                    request_id=poll_resp.file_download_request_id,
                    filename="report.pdf",
                    data=b"%PDF-sample-bytes",
                )

            task = asyncio.create_task(_client_sim())
            filename, data = await dispatcher.request_file_download(
                "C:\\docs\\report.pdf", timeout_seconds=5.0
            )
            await task

            assert filename == "report.pdf"
            assert data == b"%PDF-sample-bytes"

        asyncio.run(_test())

    def test_file_download_offline_raises(self) -> None:
        async def _test() -> None:
            config = AppConfig()
            dispatcher = AgentDispatcher(config)
            with pytest.raises(RuntimeError, match="Windows Agent не подключён"):
                await dispatcher.request_file_download("C:\\test.txt", timeout_seconds=1.0)

        asyncio.run(_test())

    def test_file_upload_flow(self) -> None:
        async def _test() -> None:
            config = AppConfig(AGENT_AUTH_SECRET="super_secret_test_key_12345")
            dispatcher = AgentDispatcher(config)

            # Simulate agent online
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="test-agent", hostname="WIN-PC")
            )

            file_content = b"print('Hello world')"

            async def _client_sim():
                await asyncio.sleep(0.05)
                # Agent polls and finds upload request
                poll_resp = await dispatcher.poll_tasks(agent_id="test-agent")
                assert poll_resp.file_upload_request_id is not None
                transfer_id = poll_resp.file_upload_request_id

                # Agent retrieves staged payload
                staged = dispatcher.get_staged_upload(transfer_id)
                assert staged is not None
                instr, payload = staged
                assert instr.filename == "hello.py"
                assert payload == file_content

                # Agent writes file and sends ack
                await dispatcher.submit_upload_ack(
                    FileUploadAck(
                        transfer_id=transfer_id,
                        success=True,
                        saved_path="C:\\dev\\hello.py",
                        bytes_written=len(payload),
                    )
                )

            task = asyncio.create_task(_client_sim())
            ack = await dispatcher.stage_file_upload(
                target_dir="C:\\dev",
                filename="hello.py",
                file_bytes=file_content,
                conflict_mode="overwrite",
                timeout_seconds=5.0,
            )
            await task

            assert ack.success is True
            assert ack.saved_path == "C:\\dev\\hello.py"
            assert ack.bytes_written == len(file_content)

        asyncio.run(_test())



class TestControllerServerFileEndpoints:
    """Integration tests for Controller REST API file transfer endpoints."""

    def test_file_download_post_endpoint(self) -> None:
        async def _test() -> None:
            secret = "test_file_transfer_secret_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            # Online registration
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="agent-pc", hostname="WIN-HOST")
            )

            file_data = b"Sample binary file payload for testing"

            async with TestClient(TestServer(app)) as client:
                # 1. Controller requests download in background
                dl_task = asyncio.create_task(
                    dispatcher.request_file_download("C:\\test.bin", timeout_seconds=5.0)
                )
                await asyncio.sleep(0.05)

                poll_resp = await dispatcher.poll_tasks("agent-pc")
                req_id = poll_resp.file_download_request_id
                assert req_id is not None

                # 2. Agent uploads file bytes to POST /api/v1/agent/files/download
                path = "/api/v1/agent/files/download"
                headers = build_auth_headers(
                    agent_id="agent-pc",
                    secret=secret,
                    method="POST",
                    path=path,
                    body=file_data,
                )
                headers["X-Request-ID"] = req_id
                headers["X-File-Name"] = quote("test.bin")
                headers["Content-Type"] = "application/octet-stream"

                resp = await client.post(path, data=file_data, headers=headers)
                assert resp.status == 200
                data = await resp.json()
                assert data["acknowledged"] is True
                assert data["received_bytes"] == len(file_data)

                # Future completes with received payload
                fname, received = await dl_task
                assert fname == "test.bin"
                assert received == file_data

        asyncio.run(_test())

    def test_file_upload_get_and_ack_endpoints(self) -> None:
        async def _test() -> None:
            secret = "test_file_transfer_secret_12345"
            config = AppConfig(AGENT_AUTH_SECRET=secret)
            dispatcher = AgentDispatcher(config)
            app = create_controller_app(config, dispatcher)

            # Online registration
            await dispatcher.handle_heartbeat(
                HeartbeatRequest(agent_id="agent-pc", hostname="WIN-HOST")
            )

            file_data = b"Some script data to save on agent"

            async with TestClient(TestServer(app)) as client:
                # 1. Controller stages upload
                upload_task = asyncio.create_task(
                    dispatcher.stage_file_upload(
                        target_dir="C:\\target\\dir",
                        filename="script.py",
                        file_bytes=file_data,
                        conflict_mode="suffix",
                        timeout_seconds=5.0,
                    )
                )
                await asyncio.sleep(0.05)

                poll_resp = await dispatcher.poll_tasks("agent-pc")
                transfer_id = poll_resp.file_upload_request_id
                assert transfer_id is not None

                # 2. Agent fetches staged upload GET /api/v1/agent/files/upload/{transfer_id}
                get_path = f"/api/v1/agent/files/upload/{transfer_id}"
                get_headers = build_auth_headers(
                    agent_id="agent-pc",
                    secret=secret,
                    method="GET",
                    path=get_path,
                )
                resp = await client.get(get_path, headers=get_headers)
                assert resp.status == 200
                assert resp.headers["X-Conflict-Mode"] == "suffix"
                assert unquote(resp.headers["X-Target-Filename"]) == "script.py"
                assert unquote(resp.headers["X-Target-Dir"]) == "C:\\target\\dir"
                payload = await resp.read()
                assert payload == file_data

                # 3. Agent confirms write POST /api/v1/agent/files/upload/{transfer_id}/ack
                ack_path = f"/api/v1/agent/files/upload/{transfer_id}/ack"
                ack_model = FileUploadAck(
                    transfer_id=transfer_id,
                    success=True,
                    saved_path="C:\\target\\dir\\script (1).py",
                    bytes_written=len(file_data),
                )
                ack_body = ack_model.model_dump_json().encode("utf-8")
                ack_headers = build_auth_headers(
                    agent_id="agent-pc",
                    secret=secret,
                    method="POST",
                    path=ack_path,
                    body=ack_body,
                )
                ack_headers["Content-Type"] = "application/json"

                ack_resp = await client.post(ack_path, data=ack_body, headers=ack_headers)
                assert ack_resp.status == 200
                ack_data = await ack_resp.json()
                assert ack_data["acknowledged"] is True

                # Controller upload_task completes with ack
                final_ack = await upload_task
                assert final_ack.success is True
                assert final_ack.saved_path == "C:\\target\\dir\\script (1).py"

        asyncio.run(_test())


class TestConflictResolutionNaming:
    """Tests for conflict resolution naming logic (Variant B)."""

    def test_suffix_generation(self, tmp_path: Path) -> None:
        # Existing original file
        orig = tmp_path / "document.pdf"
        orig.write_text("orig", encoding="utf-8")

        # Simulate suffix incrementation
        dest_path = tmp_path / "document.pdf"
        stem = dest_path.stem
        suffix = dest_path.suffix
        counter = 1
        while dest_path.exists():
            dest_path = tmp_path / f"{stem} ({counter}){suffix}"
            counter += 1

        assert dest_path.name == "document (1).pdf"
        dest_path.write_text("v1", encoding="utf-8")

        # Second conflict -> (2)
        dest_path2 = tmp_path / "document.pdf"
        counter = 1
        while dest_path2.exists():
            dest_path2 = tmp_path / f"{stem} ({counter}){suffix}"
            counter += 1

        assert dest_path2.name == "document (2).pdf"


class TestAgentRunnerFileTransfer:
    """Tests for AgentRunner download and upload routines."""

    def test_runner_handle_download_request(self, tmp_path: Path) -> None:
        async def _test() -> None:
            from unittest.mock import AsyncMock, MagicMock
            from remote_control.agent.runner import AgentRunner

            test_file = tmp_path / "data.csv"
            raw_bytes = b"a,b,c\n1,2,3"
            test_file.write_bytes(raw_bytes)

            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.upload_downloaded_file = AsyncMock(return_value=True)

            config = AppConfig()
            runner = AgentRunner(config=config, client=mock_client)

            # 1. Existing valid file
            await runner._handle_download_request("req-1", str(test_file))
            mock_client.upload_downloaded_file.assert_awaited_once_with(
                request_id="req-1",
                filename="data.csv",
                data=raw_bytes,
            )

            # 2. Non-existent file reports error
            mock_client.upload_downloaded_file.reset_mock()
            await runner._handle_download_request("req-2", str(tmp_path / "missing.txt"))
            mock_client.upload_downloaded_file.assert_awaited_once()
            call_kwargs = mock_client.upload_downloaded_file.await_args.kwargs
            assert call_kwargs["request_id"] == "req-2"
            assert "не найден" in call_kwargs["error"]

        asyncio.run(_test())


    def test_runner_handle_upload_request(self, tmp_path: Path) -> None:
        async def _test() -> None:
            from unittest.mock import AsyncMock, MagicMock
            from remote_control.agent.runner import AgentRunner

            dest_dir = tmp_path / "output"
            existing_file = dest_dir / "test.txt"
            dest_dir.mkdir(parents=True)
            existing_file.write_text("existing", encoding="utf-8")

            mock_client = MagicMock(spec=AgentHttpClient)
            mock_client.fetch_staged_upload = AsyncMock(
                return_value=(
                    {
                        "target_dir": str(dest_dir),
                        "filename": "test.txt",
                        "conflict_mode": "suffix",
                    },
                    b"new content",
                )
            )
            mock_client.acknowledge_upload = AsyncMock(return_value=True)

            config = AppConfig()
            runner = AgentRunner(config=config, client=mock_client)

            # Handle upload with conflict_mode="suffix"
            await runner._handle_upload_request("tr-999")

            # Must have written to test (1).txt
            saved_file = dest_dir / "test (1).txt"
            assert saved_file.exists()
            assert saved_file.read_text(encoding="utf-8") == "new content"
            assert existing_file.read_text(encoding="utf-8") == "existing"

            # Ack must be called with success
            mock_client.acknowledge_upload.assert_awaited_once()
            ack_arg = mock_client.acknowledge_upload.await_args[0][0]
            assert ack_arg.success is True
            assert ack_arg.saved_path == str(saved_file)
            assert ack_arg.bytes_written == len(b"new content")

        asyncio.run(_test())

