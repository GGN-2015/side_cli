"""Platform adapters for a child's interactive terminal byte stream."""

from __future__ import annotations

import asyncio
import codecs
import errno
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Protocol


class TerminalProcess(Protocol):
    pid: int

    @property
    def returncode(self) -> int | None: ...
    async def read(self, size: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def wait(self) -> int: ...
    def resize(self, columns: int, rows: int) -> None: ...
    def stop(self, signum: int) -> None: ...
    def close(self) -> None: ...


async def open_terminal(
    command: Sequence[str], size: tuple[int, int], *,
    cwd: str | os.PathLike[str] | None, env: Mapping[str, str] | None,
) -> TerminalProcess:
    if os.name == "nt":
        return _WindowsTerminal(command, size, cwd, env)
    if os.name == "posix":
        return await _PosixTerminal.start(command, size, cwd, env)
    raise RuntimeError("terminal mode requires Windows or POSIX")


def _set_size(fd: int, columns: int, rows: int) -> None:
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


_ENTER_TERMINAL = """
import fcntl, os, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
try:
    os.execvp(sys.argv[1], sys.argv[1:])
except OSError as error:
    print(f'side-cli: {error}', file=sys.stderr)
    sys.exit(127 if isinstance(error, FileNotFoundError) else 126)
"""


class _PosixTerminal:
    def __init__(self, process: asyncio.subprocess.Process, fd: int) -> None:
        self._process = process
        self.pid = process.pid
        self._fd = fd

    @classmethod
    async def start(
        cls, command: Sequence[str], size: tuple[int, int],
        cwd: str | os.PathLike[str] | None, env: Mapping[str, str] | None,
    ) -> _PosixTerminal:
        import pty

        master, slave = pty.openpty()
        try:
            os.set_blocking(master, False)
            _set_size(slave, *size)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", _ENTER_TERMINAL, *command,
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                cwd=cwd, env=env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(process, master)

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def _ready(self, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        add = loop.add_writer if write else loop.add_reader
        remove = loop.remove_writer if write else loop.remove_reader
        fd = self._fd

        def wake() -> None:
            if not ready.done():
                ready.set_result(None)

        add(fd, wake)
        try:
            await ready
        finally:
            remove(fd)

    async def read(self, size: int) -> bytes:
        while True:
            try:
                return os.read(self._fd, size)
            except BlockingIOError:
                await self._ready()
            except OSError as error:
                if error.errno == errno.EIO:
                    return b""
                raise

    async def write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            try:
                remaining = remaining[os.write(self._fd, remaining):]
            except BlockingIOError:
                await self._ready(write=True)
            except OSError as error:
                if error.errno in (errno.EIO, errno.EBADF):
                    raise BrokenPipeError("child terminal is closed") from error
                raise

    async def wait(self) -> int:
        return await self._process.wait()

    def resize(self, columns: int, rows: int) -> None:
        _set_size(self._fd, columns, rows)

    def stop(self, signum: int) -> None:
        if self._fd >= 0:
            with suppress(ProcessLookupError):
                os.killpg(self.pid, signum)

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        os.close(fd)


class _WindowsTerminal:
    def __init__(
        self, command: Sequence[str], size: tuple[int, int],
        cwd: str | os.PathLike[str] | None, env: Mapping[str, str] | None,
    ) -> None:
        from winpty import Backend, PTY, WinptyError

        path = None if env is None else env.get("PATH", os.defpath)
        executable = shutil.which(command[0], path=path)
        if executable is None:
            raise FileNotFoundError(f"command not found: {command[0]}")
        if os.path.splitext(executable)[1].lower() in {".bat", ".cmd"}:
            raise ValueError("terminal mode requires an executable; run batch files via cmd.exe")
        self._winpty_error = WinptyError
        environment = None if env is None else "\0".join(
            f"{name}={value}" for name, value in sorted(env.items(), key=lambda item: item[0].upper())
        ) + "\0"
        cmdline = " " + subprocess.list2cmdline(command[1:]) if len(command) > 1 else None
        try:
            self._pty = PTY(*size, backend=Backend.ConPTY)
            spawned = self._pty.spawn(
                executable, cwd=os.fspath(cwd) if cwd is not None else None,
                env=environment, cmdline=cmdline,
            )
        except WinptyError as error:
            raise OSError(f"could not start {executable} in ConPTY: {error}") from error
        if not spawned:
            raise OSError(f"could not start {executable} in ConPTY")
        self.pid = self._pty.pid
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = b""
        self._exit_code = None

    @property
    def returncode(self) -> int | None:
        return self._pty.get_exitstatus() if self._pty is not None else self._exit_code

    async def read(self, size: int) -> bytes:
        while not self._pending:
            try:
                text = self._pty.read(blocking=False)
            except self._winpty_error as error:
                if self._pty.iseof():
                    return b""
                raise OSError(f"ConPTY read failed: {error}") from error
            if text:
                self._pending = text.encode("utf-8")
                break
            if self._pty.iseof():
                return b""
            await asyncio.sleep(0.01)
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    async def write(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        for offset in range(0, len(text), 4096):
            pty = self._pty
            if pty is None:
                raise BrokenPipeError("child terminal is closed")
            try:
                await asyncio.to_thread(pty.write, text[offset:offset + 4096])
            except self._winpty_error as error:
                raise BrokenPipeError(str(error)) from error

    async def wait(self) -> int:
        while (exit_code := self.returncode) is None:
            await asyncio.sleep(0.01)
        return exit_code

    def resize(self, columns: int, rows: int) -> None:
        try:
            self._pty.set_size(columns, rows)
        except self._winpty_error as error:
            if self.returncode is None:
                raise OSError(f"ConPTY resize failed: {error}") from error

    def stop(self, signum: int) -> None:
        if self.returncode is None and self._pty is not None:
            with suppress(ProcessLookupError):
                os.kill(self.pid, signum)

    def close(self) -> None:
        self._exit_code = self.returncode
        pty, self._pty = self._pty, None
        with suppress(self._winpty_error):
            pty.cancel_io()
