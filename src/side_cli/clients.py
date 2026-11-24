"""Async clients for the framed output and raw input listeners."""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from collections.abc import AsyncIterator
from contextlib import suppress
from types import TracebackType

from .supervisor import CHUNK_SIZE, HOST, _FRAME_HEADER, _OUTPUT_STREAM_IDS, Ports, Stream

_OUTPUT_STREAMS = {identifier: stream for stream, identifier in _OUTPUT_STREAM_IDS.items()}
_ABORTIVE_LINGER = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("client port must be an integer between 1 and 65535")


class PanelClient:
    """Connect to a local framed output port and iterate over (stream, bytes) pairs."""

    def __init__(self, port: int = Ports().stdout) -> None:
        _validate_port(port)
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> PanelClient:
        if self._writer is not None:
            raise RuntimeError("panel client is already connected")
        self._reader, self._writer = await asyncio.open_connection(HOST, self.port)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def __aiter__(self) -> AsyncIterator[tuple[Stream, bytes]]:
        reader = self._reader
        if reader is None:
            raise RuntimeError("panel client is not connected")
        while True:
            try:
                header = await reader.readexactly(_FRAME_HEADER.size)
            except asyncio.IncompleteReadError as error:
                if not error.partial:
                    return
                raise ValueError("incomplete output frame header") from error

            identifier, length = _FRAME_HEADER.unpack(header)
            stream = _OUTPUT_STREAMS.get(identifier)
            if stream is None or not 1 <= length <= CHUNK_SIZE:
                raise ValueError("invalid output frame header")
            try:
                data = await reader.readexactly(length)
            except asyncio.IncompleteReadError as error:
                raise ValueError("incomplete output frame payload") from error
            yield stream, data


class InputClient:
    """Send bytes to a local stdin listener. Use an async context to connect."""

    def __init__(self, port: int = Ports().stdin) -> None:
        _validate_port(port)
        self.port = port
        self._socket: socket.socket | None = None
        self._send_lock = asyncio.Lock()

    async def __aenter__(self) -> InputClient:
        if self._socket is not None:
            raise RuntimeError("input client is already connected")
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            connection.setblocking(False)
            await asyncio.get_running_loop().sock_connect(connection, (HOST, self.port))
        except BaseException:
            connection.close()
            raise
        self._socket = connection
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(abort=exc_type is not None)

    def close(self, *, abort: bool = False) -> None:
        """Disconnect. Abort skips the orderly EOF that can close the child's stdin."""
        connection, self._socket = self._socket, None
        if connection is not None:
            try:
                if abort:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _ABORTIVE_LINGER)
            finally:
                connection.close()

    async def send(self, data: bytes) -> None:
        """Send raw bytes without modifying them."""
        async with self._send_lock:
            await asyncio.get_running_loop().sock_sendall(self._connected_socket(), data)

    async def send_line(self, data: bytes) -> None:
        """Send bytes, adding a trailing LF if missing."""
        await self.send(data if data.endswith(b"\n") else data + b"\n")

    async def wait_for_disconnect(self) -> None:
        """Wait for the server to close the connection; raise on a socket error."""
        connection = self._connected_socket()
        while await asyncio.get_running_loop().sock_recv(connection, CHUNK_SIZE):
            pass

    def _connected_socket(self) -> socket.socket:
        if self._socket is None:
            raise RuntimeError("input client is not connected")
        return self._socket
