"""Temporarily put the user's terminal in byte-forwarding mode."""

from __future__ import annotations

import asyncio
import codecs
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError
from contextlib import ExitStack, suppress


def terminal_size() -> tuple[int, int] | None:
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        with suppress(OSError, AttributeError, ValueError):
            size = os.get_terminal_size(stream.fileno())
            if size.columns and size.lines:
                return size.columns, size.lines
    return None


class LocalTerminal(ExitStack):
    def __init__(self, *, local_input: bool) -> None:
        super().__init__()
        self._local_input = local_input
        self._windows = None

    def __enter__(self) -> LocalTerminal:
        try:
            if os.name == "nt":
                self._windows = _WindowsConsole(self, self._local_input)
            else:
                self._configure_posix()
        except BaseException:
            self.close()
            raise
        return self

    def _configure_posix(self) -> None:
        import termios
        import tty

        for stream, raw_input in ((sys.stdin, self._local_input), (sys.stdout, False)):
            if stream is sys.stdin and not raw_input:
                continue
            if stream is not None and stream.isatty():
                fd = stream.fileno()
                mode = termios.tcgetattr(fd)
                self.callback(termios.tcsetattr, fd, termios.TCSANOW, mode.copy())
                if raw_input:
                    tty.setraw(fd, termios.TCSANOW)
                else:
                    mode[1] &= ~termios.OPOST
                    termios.tcsetattr(fd, termios.TCSANOW, mode)

    def start_input(self, send: Callable[[bytes], Awaitable[None]]) -> None:
        if not self._local_input or sys.stdin is None:
            return
        fd = sys.stdin.fileno()
        loop = asyncio.get_running_loop()
        stopped = threading.Event()

        async def deliver(chunk: bytes) -> None:
            if not stopped.is_set():
                await send(chunk)

        def forward() -> None:
            while not stopped.is_set():
                try:
                    if self._windows is not None:
                        data = self._windows.read(fd)
                    else:
                        from select import select

                        if not select([fd], [], [], 0.05)[0] or stopped.is_set():
                            continue
                        data = os.read(fd, 4096)
                    if data is None:
                        continue
                    if not data or stopped.is_set():
                        return

                    operation = deliver(data)
                    try:
                        future = asyncio.run_coroutine_threadsafe(operation, loop)
                    except RuntimeError:
                        operation.close()
                        return
                    while not stopped.is_set():
                        try:
                            future.result(timeout=0.05)
                            break
                        except TimeoutError:
                            continue
                    if stopped.is_set():
                        future.cancel()
                except (OSError, ValueError, CancelledError):
                    return

        reader = threading.Thread(target=forward, name="side-cli-terminal-input", daemon=True)
        reader.start()

        def stop() -> None:
            stopped.set()
            if self._windows is not None:
                self._windows.cancel(reader)
            reader.join(timeout=0.15)

        self.callback(stop)


class _WindowsConsole:
    def __init__(self, stack: ExitStack, local_input: bool) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self._ctypes = ctypes
        self._types = wintypes
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.GetConsoleMode.argtypes = wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        self._kernel.SetConsoleMode.argtypes = wintypes.HANDLE, wintypes.DWORD
        self._kernel.ReadConsoleW.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        self._kernel.OpenThread.argtypes = wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        self._kernel.OpenThread.restype = wintypes.HANDLE
        self._kernel.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
        self._kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._input_handle = None
        self._decoder = codecs.getincrementaldecoder("utf-16-le")("replace")

        for stream, is_input in ((sys.stdin, True), (sys.stdout, False)):
            if stream is None or (is_input and not local_input):
                continue
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = wintypes.DWORD()
            if not self._kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if is_input:
                processed, line, echo, quick_edit = 0x0001, 0x0002, 0x0004, 0x0040
                extended, vt_input = 0x0080, 0x0200
                new_mode = (mode.value & ~(processed | line | echo | quick_edit)) | extended | vt_input
                self._input_handle = handle
            else:
                processed, vt_output, no_auto_return = 0x0001, 0x0004, 0x0008
                new_mode = mode.value | processed | vt_output | no_auto_return
            if not self._kernel.SetConsoleMode(handle, new_mode):
                raise ctypes.WinError(ctypes.get_last_error())
            stack.callback(self._kernel.SetConsoleMode, handle, mode.value)

    def read(self, fd: int) -> bytes | None:
        if self._input_handle is None:
            return os.read(fd, 4096)
        buf = self._ctypes.create_string_buffer(4096 * 2)
        count = self._types.DWORD()
        if not self._kernel.ReadConsoleW(self._input_handle, buf, 4096, self._ctypes.byref(count), None):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        text = self._decoder.decode(buf.raw[:count.value * 2])
        return text.encode("utf-8") if text else None

    def cancel(self, thread: threading.Thread) -> None:
        if thread.is_alive() and thread.native_id is not None:
            thread_terminate = 0x0001
            handle = self._kernel.OpenThread(thread_terminate, False, thread.native_id)
            if handle:
                try:
                    self._kernel.CancelSynchronousIo(handle)
                finally:
                    self._kernel.CloseHandle(handle)
