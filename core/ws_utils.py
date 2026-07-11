"""
core/ws_utils.py
Shared WebSocket helpers for the Binance market-data streams.
"""

from __future__ import annotations

import socket


def apply_keepalive(ws) -> None:
    """Applies TCP keepalive to an already-connected WebSocket.

    Called AFTER websockets.connect() succeeds. Gets the underlying socket
    from the transport and sets SO_KEEPALIVE + platform-specific intervals.
    This avoids the Windows WinError 10057 that occurs when passing an
    unconnected socket to websockets.connect(sock=...).

    Prevents NAT/firewall idle-timeout disconnects (~300-360s) by sending
    TCP-level ACK probes every 60s.

    P3.1 consolidation: single copy shared by 1_data_ingestion,
    19_whale_logger_bot and chart_data_service (were three byte-identical copies).
    """
    # Local import: keeps mypy (platform=win32) from narrowing sys.platform and
    # flagging the POSIX branch as unreachable — the reason the three original
    # copies imported sys inside the function.
    import sys

    try:
        sock = ws.transport.get_extra_info("socket")
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "win32":
            # SIO_KEEPALIVE_VALS: (onoff, keepalivetime_ms, keepaliveinterval_ms)
            # First probe after 60s idle, then every 10s
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 10_000))
        else:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
    except (AttributeError, OSError):
        pass  # Non-fatal — connection still works without keepalive
