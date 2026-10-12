"""Unit tests for Wake-on-LAN Magic Packet generation, network transmission, and /wake handler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, User

from remote_control.common.wol import (
    check_host_availability_sync,
    create_magic_packet,
    parse_mac_address,
    send_magic_packet_sync,
    send_wake_on_lan,
    wait_for_host,
)
from remote_control.config import AppConfig
from remote_control.controller.handlers.base import cmd_wake


class TestMagicPacketGeneration:
    """Tests for MAC address parsing and Magic Packet bytes construction."""

    @pytest.mark.parametrize(
        "mac_str,expected_bytes",
        [
            ("AA:BB:CC:DD:EE:FF", b"\xaa\xbb\xcc\xdd\xee\xff"),
            ("aa-bb-cc-dd-ee-ff", b"\xaa\xbb\xcc\xdd\xee\xff"),
            ("AABBCCDDEEFF", b"\xaa\xbb\xcc\xdd\xee\xff"),
            ("aabb.ccdd.eeff", b"\xaa\xbb\xcc\xdd\xee\xff"),
            ("00:11:22:33:44:55", b"\x00\x11\x22\x33\x44\x55"),
        ],
    )
    def test_parse_mac_address_valid_formats(self, mac_str: str, expected_bytes: bytes) -> None:
        assert parse_mac_address(mac_str) == expected_bytes

    @pytest.mark.parametrize(
        "invalid_mac",
        [
            "",
            "AA:BB:CC:DD:EE",  # Too short
            "AA:BB:CC:DD:EE:FF:11",  # Too long
            "GG:HH:II:JJ:KK:LL",  # Non-hex characters
            "not-a-mac-address",
        ],
    )
    def test_parse_mac_address_invalid(self, invalid_mac: str) -> None:
        with pytest.raises(ValueError):
            parse_mac_address(invalid_mac)

    def test_create_magic_packet_exact_structure(self) -> None:
        mac = "01:23:45:67:89:AB"
        mac_bytes = bytes.fromhex("0123456789AB")
        packet = create_magic_packet(mac)

        assert len(packet) == 102
        assert packet[:6] == b"\xff\xff\xff\xff\xff\xff"
        # Remaining 96 bytes must be 16 repetitions of mac_bytes
        for i in range(16):
            start = 6 + i * 6
            assert packet[start : start + 6] == mac_bytes


class TestSendMagicPacket:
    """Tests for UDP packet transmission and socket configuration."""

    def test_send_magic_packet_sync_transmits_multiple_times(self) -> None:
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock

            sent = send_magic_packet_sync(
                mac="AA:BB:CC:DD:EE:FF",
                broadcast_ip="192.168.1.255",
                port=9,
                repeat=3,
                interval=0.01,
            )

            assert sent == 3
            assert mock_sock.sendto.call_count == 3
            call_args = mock_sock.sendto.call_args[0]
            assert len(call_args[0]) == 102  # magic packet length
            assert call_args[1] == ("192.168.1.255", 9)
            mock_sock.close.assert_called_once()

    def test_send_wake_on_lan_async(self) -> None:
        async def _test() -> None:
            with patch(
                "remote_control.common.wol.send_magic_packet_sync", return_value=3
            ) as mock_sync:
                sent = await send_wake_on_lan(mac="AA:BB:CC:DD:EE:FF")
                assert sent == 3
                mock_sync.assert_called_once()

        asyncio.run(_test())


class TestHostAvailabilityAndPolling:
    """Tests for TCP socket check and polling with backoff."""

    def test_check_host_availability_success(self) -> None:
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.connect.return_value = None

            assert check_host_availability_sync("127.0.0.1", 8080) is True
            mock_sock.close.assert_called_once()

    def test_check_host_availability_failure(self) -> None:
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.connect.side_effect = OSError("Connection refused")

            assert check_host_availability_sync("127.0.0.1", 8080) is False
            mock_sock.close.assert_called_once()

    def test_wait_for_host_polling_succeeds_eventually(self) -> None:
        async def _test() -> None:
            attempts = [False, False, True]

            async def fake_check(host: str, port: int, timeout: float = 2.0) -> bool:
                return attempts.pop(0)

            with patch(
                "remote_control.common.wol.check_host_availability", side_effect=fake_check
            ):
                result = await wait_for_host(
                    "127.0.0.1",
                    8080,
                    max_wait_seconds=5.0,
                    initial_delay=0.01,
                    check_interval=0.01,
                )
                assert result is True

        asyncio.run(_test())

    def test_wait_for_host_times_out(self) -> None:
        async def _test() -> None:
            with patch("remote_control.common.wol.check_host_availability", return_value=False):
                result = await wait_for_host(
                    "127.0.0.1",
                    8080,
                    max_wait_seconds=0.05,
                    initial_delay=0.01,
                    check_interval=0.01,
                )
                assert result is False

        asyncio.run(_test())


class TestTelegramWakeHandler:
    """Tests for aiogram /wake command handler."""

    def _make_msg(self) -> Message:
        mock_user = User(id=111222333, is_bot=False, first_name="Kostya")
        mock_chat = Chat(id=111222333, type="private")
        msg = MagicMock(spec=Message)
        msg.from_user = mock_user
        msg.chat = mock_chat
        msg.answer = AsyncMock()
        return msg

    def test_wake_without_mac_shows_instructions(self) -> None:
        async def _test() -> None:
            msg = self._make_msg()
            cfg = AppConfig(
                ALLOWED_TELEGRAM_USERS=[111222333],
                TARGET_MAC_ADDRESS=None,
            )
            await cmd_wake(msg, config=cfg)

            msg.answer.assert_awaited_once()
            text = msg.answer.call_args[0][0]
            assert "MAC-адрес целевого ПК не настроен" in text
            assert "TARGET_MAC_ADDRESS" in text

        asyncio.run(_test())

    def test_wake_when_already_online(self) -> None:
        async def _test() -> None:
            msg = self._make_msg()
            cfg = AppConfig(
                ALLOWED_TELEGRAM_USERS=[111222333],
                TARGET_MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
            )
            with patch(
                "remote_control.controller.handlers.base.check_host_availability",
                return_value=True,
            ):
                await cmd_wake(msg, config=cfg)

                msg.answer.assert_awaited_once()
                text = msg.answer.call_args[0][0]
                assert "уже включён и доступен" in text

        asyncio.run(_test())

    def test_wake_packet_sent_and_host_wakes_up(self) -> None:
        async def _test() -> None:
            msg = self._make_msg()
            status_msg = MagicMock()
            status_msg.edit_text = AsyncMock()
            msg.answer.return_value = status_msg

            cfg = AppConfig(
                ALLOWED_TELEGRAM_USERS=[111222333],
                TARGET_MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
            )
            with (
                patch(
                    "remote_control.controller.handlers.base.check_host_availability",
                    return_value=False,
                ),
                patch(
                    "remote_control.controller.handlers.base.send_wake_on_lan",
                    return_value=3,
                ) as mock_send,
                patch(
                    "remote_control.controller.handlers.base.wait_for_host",
                    return_value=True,
                ) as mock_wait,
            ):
                await cmd_wake(msg, config=cfg)

                mock_send.assert_awaited_once()
                mock_wait.assert_awaited_once()
                assert status_msg.edit_text.await_count == 2
                final_text = status_msg.edit_text.call_args[0][0]
                assert "успешно проснулся и готов к работе" in final_text

        asyncio.run(_test())

    def test_wake_packet_sent_and_times_out(self) -> None:
        async def _test() -> None:
            msg = self._make_msg()
            status_msg = MagicMock()
            status_msg.edit_text = AsyncMock()
            msg.answer.return_value = status_msg

            cfg = AppConfig(
                ALLOWED_TELEGRAM_USERS=[111222333],
                TARGET_MAC_ADDRESS="AA:BB:CC:DD:EE:FF",
            )
            with (
                patch(
                    "remote_control.controller.handlers.base.check_host_availability",
                    return_value=False,
                ),
                patch(
                    "remote_control.controller.handlers.base.send_wake_on_lan",
                    return_value=3,
                ),
                patch(
                    "remote_control.controller.handlers.base.wait_for_host",
                    return_value=False,
                ),
            ):
                await cmd_wake(msg, config=cfg)

                final_text = status_msg.edit_text.call_args[0][0]
                assert "Таймаут ожидания пробуждения" in final_text

        asyncio.run(_test())
