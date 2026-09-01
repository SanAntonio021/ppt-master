"""Block Python socket use when the CC Switch offline smoke explicitly enables it."""

from __future__ import annotations

import os
import socket


if os.environ.get("PPT_MASTER_BLOCK_NETWORK") == "1":
    def _blocked(*_args, **_kwargs):
        raise RuntimeError("PPT_MASTER_OFFLINE_BLOCKED")


    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked
    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
