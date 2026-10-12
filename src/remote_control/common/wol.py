"""Wake-on-LAN (WOL) implementation: Magic Packet generation, UDP broadcast, and host check."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time

logger = logging.getLogger(__name__)

_MAC_CLEAN_REGEX = re.compile(r"[^0-9a-fA-F]")


def parse_mac_address(mac: str) -> bytes:
    """Parse and validate MAC address string in various formats (colons, hyphens, dots, plain hex).

    Returns:
        6 bytes representing the hardware MAC address.

    Raises:
        ValueError: If MAC address string is invalid or does not contain exactly 6 octets.
    """
    clean_hex = _MAC_CLEAN_REGEX.sub("", mac)
    if len(clean_hex) != 12:
        raise ValueError(
            f"Invalid MAC address '{mac}'. Expected 12 hexadecimal characters (6 bytes)."
        )

    try:
        return bytes.fromhex(clean_hex)
    except ValueError as e:
        raise ValueError(f"Invalid MAC address characters in '{mac}': {e}") from e


def create_magic_packet(mac: str) -> bytes:
    """Generate a 102-byte Wake-on-LAN Magic Packet for the given MAC address.

    Format:
        6 bytes of 0xFF synchronization sequence,
        followed by 16 repetitions of the 6-byte target MAC address.
    """
    mac_bytes = parse_mac_address(mac)
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet_sync(
    mac: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = 9,
    repeat: int = 3,
    interval: float = 0.1,
) -> int:
    """Synchronously send Wake-on-LAN Magic Packet via UDP broadcast.

    Repeats transmission multiple times for reliability in noisy or wireless networks.

    Returns:
        The number of successfully transmitted packets.
    """
    packet = create_magic_packet(mac)
    sent_count = 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for i in range(repeat):
            try:
                sock.sendto(packet, (broadcast_ip, port))
                sent_count += 1
            except OSError as e:
                logger.warning(
                    "Failed sending WOL packet #%d to %s:%d: %s",
                    i + 1,
                    broadcast_ip,
                    port,
                    e,
                )

            if i < repeat - 1 and interval > 0:
                time.sleep(interval)
    finally:
        sock.close()

    logger.info(
        "Sent %d Wake-on-LAN Magic Packet(s) to %s:%d for MAC %s",
        sent_count,
        broadcast_ip,
        port,
        mac,
    )
    return sent_count


async def send_wake_on_lan(
    mac: str,
    broadcast_ip: str = "255.255.255.255",
    port: int = 9,
    repeat: int = 3,
    interval: float = 0.1,
) -> int:
    """Asynchronously transmit Wake-on-LAN Magic Packet via background thread."""
    return await asyncio.to_thread(
        send_magic_packet_sync,
        mac=mac,
        broadcast_ip=broadcast_ip,
        port=port,
        repeat=repeat,
        interval=interval,
    )


def check_host_availability_sync(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if host is reachable by testing a TCP connection to the specified port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (TimeoutError, OSError):
        return False
    finally:
        sock.close()


async def check_host_availability(host: str, port: int, timeout: float = 2.0) -> bool:
    """Asynchronously check if host is reachable via TCP connect."""
    return await asyncio.to_thread(check_host_availability_sync, host, port, timeout)


async def wait_for_host(
    host: str,
    port: int,
    max_wait_seconds: float = 60.0,
    initial_delay: float = 1.0,
    check_interval: float = 2.0,
) -> bool:
    """Poll host availability until it responds or max_wait_seconds elapses."""
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)

    start_time = time.monotonic()
    while time.monotonic() - start_time < max_wait_seconds:
        if await check_host_availability(host, port, timeout=1.5):
            return True
        await asyncio.sleep(check_interval)

    return False
