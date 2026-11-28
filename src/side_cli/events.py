"""Versioned, read-only terminal events with replay from the start of a session."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from contextlib import suppress
from enum import IntEnum
from tempfile import TemporaryFile

_MAGIC = b"SIDEPTY\x01"
_HEADER = struct.Struct("!BI")
_SIZE = struct.Struct("!HH")
_BATCH_SIZE = 64 * 1024
MAX_LINE_BYTES = 1024 * 1024


class Event(IntEnum):
    DATA = 1
    SIZE = 2
    LINE = 3
    EXIT = 4
    LIVE = 5


class LineSource(IntEnum):
    GUI = 1
    TCP = 2


_LIMITS = {
    Event.DATA: (1, _BATCH_SIZE), Event.SIZE: (_SIZE.size, _SIZE.size),
    Event.LINE: (1, MAX_LINE_BYTES), Event.EXIT: (1, 20), Event.LIVE: (0, 0),
}


def is_terminal_line(text: str) -> bool:
    return not any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in text)


def _frame(kind: Event, data: bytes) -> bytes:
    return _HEADER.pack(kind, len(data)) + data


class TerminalJournal:
    def __init__(self) -> None:
        self._file = TemporaryFile(mode="w+b")
        self._end = 0
        self._changed = asyncio.Event()
        self.finished = False

    def append(self, kind: Event, data: bytes) -> None:
        self._file.seek(self._end)
        self._end += self._file.write(_frame(kind, data))
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def resize(self, columns: int, rows: int) -> None:
        self.append(Event.SIZE, _SIZE.pack(columns, rows))

    def finish(self, code: int) -> None:
        self.append(Event.EXIT, str(code).encode("ascii"))
        self.finished = True

    def close(self) -> None:
        self._file.close()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        playback = asyncio.create_task(self._playback(writer))
        disconnect = asyncio.create_task(reader.read(1))
        try:
            done, _ = await asyncio.wait((playback, disconnect), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            playback.cancel()
            disconnect.cancel()
            await asyncio.gather(playback, disconnect, return_exceptions=True)

    async def _playback(self, writer: asyncio.StreamWriter) -> None:
        offset, catchup = 0, self._end
        writer.write(_MAGIC)
        while True:
            if offset == catchup:
                writer.write(_frame(Event.LIVE, b""))
                catchup = -1
            end = min(self._end, catchup) if catchup >= 0 else self._end
            if offset < end:
                self._file.seek(offset)
                chunk = self._file.read(min(_BATCH_SIZE, end - offset))
                offset += len(chunk)
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0)
            else:
                await writer.drain()
                if self.finished:
                    return
                if offset == self._end:
                    await self._changed.wait()


async def terminal_events(host: str, port: int) -> AsyncIterator[tuple[Event, bytes]]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        async with asyncio.timeout(10):
            if await reader.readexactly(len(_MAGIC)) != _MAGIC:
                raise ValueError("not a supported terminal event port")
        while True:
            try:
                header = await reader.readexactly(_HEADER.size)
            except asyncio.IncompleteReadError as error:
                if not error.partial:
                    return
                raise
            identifier, length = _HEADER.unpack(header)
            kind = Event(identifier)
            minimum, maximum = _LIMITS[kind]
            if not minimum <= length <= maximum:
                raise ValueError("invalid terminal event length")
            yield kind, await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise ValueError("incomplete terminal event stream") from error
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def terminal_dimensions(data: bytes) -> tuple[int, int]:
    columns, rows = _SIZE.unpack(data)
    if not (2 <= columns <= 4096 and 1 <= rows <= 4096):
        raise ValueError("invalid terminal dimensions")
    return columns, rows
