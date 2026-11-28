"""Shared subprocess and TCP implementation."""

from __future__ import annotations

import asyncio
import codecs
import inspect
import os
import signal
import struct
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Literal

from .terminal import TerminalProcess, open_terminal

HOST = "127.0.0.1"
CHUNK_SIZE = 64 * 1024
CLIENT_BUFFER_LIMIT = 4 * 1024 * 1024
INPUT_LINE_LIMIT = 1024 * 1024
SHUTDOWN_TIMEOUT = 2.0
_CONPTY_ENTER_DELAY = 0.2


class Stream(StrEnum):
    STDIN = "stdin"
    STDOUT = "stdout"
    STDERR = "stderr"


Endpoint = Stream | Literal["output"]
_FRAME_HEADER = struct.Struct("!BI")
_OUTPUT_STREAM_IDS = {Stream.STDOUT: 1, Stream.STDERR: 2}
_CLINK_PROMPT_TAG = "C\bL\bI\bN\bK\b \b"


@dataclass(frozen=True, slots=True)
class Ports:
    """TCP ports; zero requests an available port. Output overrides stdout and stderr."""

    stdin: int = 8765
    stdout: int = 8766
    stderr: int = 8767
    output: int | None = None

    def __post_init__(self) -> None:
        ports = self._bindings().values()
        if any(type(port) is not int or not 0 <= port <= 65535 for port in ports):
            raise ValueError("ports must be integers between 0 and 65535")
        fixed = [port for port in ports if port]
        if len(set(fixed)) != len(fixed):
            raise ValueError("active TCP listeners must use different ports")

    def _bindings(self, terminal: bool = False) -> dict[Endpoint, int]:
        if self.output is not None:
            return {Stream.STDIN: self.stdin, "output": self.output}
        if terminal:
            return {Stream.STDIN: self.stdin, Stream.STDOUT: self.stdout}
        return {Stream.STDIN: self.stdin, Stream.STDOUT: self.stdout, Stream.STDERR: self.stderr}


@dataclass(frozen=True, slots=True)
class PortStatus:
    stream: Endpoint
    host: str
    port: int
    listening: bool
    clients: int


OutputHandler = Callable[[Stream, bytes], Awaitable[None] | None]
InputEcho = Callable[[str], Awaitable[None] | None]


class _InputLines:
    """Assemble LF-terminated byte frames for one input source."""

    def __init__(self) -> None:
        self.pending = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        offset = 0
        while offset < len(data):
            newline = data.find(b"\n", offset)
            end = len(data) if newline == -1 else newline + 1
            if len(self.pending) + end - offset > INPUT_LINE_LIMIT:
                raise ValueError("input line exceeds 1 MiB")
            self.pending.extend(data[offset:end])
            if newline == -1:
                return
            line = bytes(self.pending)
            self.pending.clear()
            offset = end
            yield line


def _piped_environment(
    executable: str, env: Mapping[str, str] | None,
) -> Mapping[str, str] | None:
    """Remove Clink's inherited prompt tag when CMD cannot use its console hooks."""
    if os.name != "nt" or os.path.basename(executable).casefold() not in {"cmd", "cmd.exe"}:
        return env

    source = os.environ if env is None else env
    updates = {
        name: value.removeprefix(_CLINK_PROMPT_TAG)
        for name, value in source.items()
        if name.upper() == "PROMPT" and value.startswith(_CLINK_PROMPT_TAG)
    }
    return {**source, **updates} if updates else env


class Supervisor:
    """Run one command with pipes or a terminal on the current asyncio loop."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        ports: Ports = Ports(),
        on_output: OutputHandler | None = None,
        on_input_echo: InputEcho | None = None,
        input_encoding: str = "utf-8",
        close_stdin_on_eof: bool = False,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        terminal_size: tuple[int, int] | None = None,
    ) -> None:
        if isinstance(command, str) or not command or not command[0]:
            raise ValueError("command must be a nonempty argument sequence")
        self.command = tuple(command)
        if terminal_size is not None and close_stdin_on_eof:
            raise ValueError("terminal input cannot be closed independently of terminal output")
        self._terminal_size = terminal_size
        self._port_bindings = ports._bindings(terminal_size is not None)
        self._merged_output = ports.output is not None
        self._bound_ports: dict[Endpoint, int] = {}
        self._servers: dict[Endpoint, asyncio.Server] = {}
        self._clients: dict[Endpoint, set[asyncio.StreamWriter]] = {
            endpoint: set() for endpoint in self._port_bindings
        }
        self._client_tasks: set[asyncio.Task[None]] = set()
        self._connections_changed = asyncio.Event()
        self._started = asyncio.Event()
        self._stdin_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._terminal: TerminalProcess | None = None
        self._completion: asyncio.Task[int] | None = None
        self._on_output = on_output
        self._on_input_echo = on_input_echo
        self._input_decoder = codecs.getincrementaldecoder(input_encoding) if on_input_echo else None
        self._close_stdin_on_eof = close_stdin_on_eof
        self._cwd = cwd
        self._env = env
        self._closed = False

    async def __aenter__(self) -> Supervisor:
        await self.listen()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    @property
    def pid(self) -> int | None:
        child = self._terminal or self._process
        return None if child is None else child.pid

    @property
    def returncode(self) -> int | None:
        child = self._terminal or self._process
        return None if child is None else child.returncode

    @property
    def stdin_open(self) -> bool:
        if self._terminal is not None:
            return self._terminal.returncode is None and not self._closed
        return (
            self._process is not None
            and self._process.returncode is None
            and self._process.stdin is not None
            and not self._process.stdin.is_closing()
        )

    def status(self) -> dict[Endpoint, PortStatus]:
        """Return a snapshot of the bound ports and active client counts."""
        return {
            endpoint: PortStatus(
                endpoint,
                HOST,
                self._bound_ports.get(endpoint, port),
                endpoint in self._servers and self._servers[endpoint].is_serving(),
                len(self._clients[endpoint]),
            )
            for endpoint, port in self._port_bindings.items()
        }

    async def listen(self) -> None:
        """Bind the configured ports without launching the command."""
        if self._closed:
            raise RuntimeError("supervisor is closed")
        if self._servers:
            return
        try:
            for endpoint, port in self._port_bindings.items():
                def connected(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    endpoint: Endpoint = endpoint,
                ) -> None:
                    self._connect(endpoint, reader, writer)

                server = await asyncio.start_server(
                    connected, HOST, port, start_serving=False
                )
                self._servers[endpoint] = server
                self._bound_ports[endpoint] = server.sockets[0].getsockname()[1]
            for server in self._servers.values():
                await server.start_serving()
        except BaseException:
            await self.aclose()
            raise

    async def wait_for_output_clients(self) -> None:
        """Wait until each output port has at least one connected reader."""
        while self._servers:
            if all(clients for endpoint, clients in self._clients.items() if endpoint is not Stream.STDIN):
                return
            self._connections_changed.clear()
            await self._connections_changed.wait()
        raise RuntimeError("output ports are not listening")

    async def start(self) -> None:
        """Bind the listeners if necessary and launch the command without a shell."""
        if self.pid is not None:
            raise RuntimeError("the command has already been started")
        await self.listen()
        try:
            if self._terminal_size is not None:
                self._terminal = await open_terminal(
                    self.command, self._terminal_size, cwd=self._cwd, env=self._env,
                )
                self._completion = asyncio.create_task(self._run_terminal(self._terminal))
            else:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._cwd,
                    env=_piped_environment(self.command[0], self._env),
                    start_new_session=os.name == "posix",
                )
                self._completion = asyncio.create_task(self._run(self._process))
        except BaseException:
            await self.aclose()
            raise
        self._started.set()

    async def wait(self) -> int:
        """Return the child's exit code after draining stdout and stderr."""
        if self._completion is None:
            raise RuntimeError("the command has not been started")
        return await asyncio.shield(self._completion)

    async def send(self, data: bytes) -> None:
        """Write bytes to the child's stdin, or raise BrokenPipeError if closed."""
        async with self._stdin_lock:
            if not self.stdin_open:
                raise BrokenPipeError("child stdin is not open")
            if self._terminal is not None:
                await self._terminal.write(data)
                return
            assert self._process is not None
            writer = self._process.stdin
            assert writer is not None
            writer.write(data)
            await writer.drain()

    async def send_terminal_line(self, data: bytes) -> None:
        """Submit text and Enter as one input, pacing the Enter key on ConPTY."""
        async with self._stdin_lock:
            if self._terminal is None or not self.stdin_open:
                raise BrokenPipeError("child terminal is not open")
            if os.name == "nt" and data:
                await self._terminal.write(data)
                await asyncio.sleep(_CONPTY_ENTER_DELAY)
                data = b""
            await self._terminal.write(data + b"\r")

    async def close_stdin(self, *, final_data: bytes = b"") -> None:
        """Write optional final bytes and send EOF after all preceding writes."""
        if self._terminal_size is not None:
            raise RuntimeError("terminal input cannot be closed independently of terminal output")
        async with self._stdin_lock:
            writer = self._process.stdin if self._process is not None else None
            if writer is None or writer.is_closing():
                if final_data:
                    raise BrokenPipeError("child stdin is not open")
                return
            if final_data:
                writer.write(final_data)
            writer.close()

    def resize_terminal(self, columns: int, rows: int) -> None:
        """Resize a running terminal, or set its initial size before launch."""
        if self._terminal_size is None:
            raise RuntimeError("not running in terminal mode")
        self._terminal_size = (columns, rows)
        if self._terminal is not None and self.stdin_open:
            self._terminal.resize(columns, rows)

    def forward_console_interrupt(self) -> None:
        """Relay a console interrupt if the child does not receive it natively."""
        if os.name == "posix":
            self._stop(signal.SIGINT)
        elif self._terminal is not None:
            async def interrupt() -> None:
                with suppress(OSError):
                    await self.send(b"\x03")

            asyncio.get_running_loop().create_task(interrupt())

    def terminate(self) -> None:
        """Terminate the child (and its process group on POSIX)."""
        self._stop(signal.SIGTERM)

    async def aclose(self) -> None:
        """Stop a running command and release sockets; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._completion is not None and not self._completion.done():
            self.terminate()
            try:
                await asyncio.wait_for(self.wait(), SHUTDOWN_TIMEOUT)
            except TimeoutError:
                self._stop(signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.wait(), SHUTDOWN_TIMEOUT)
                except TimeoutError:
                    self._completion.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._completion
        await self._close_network()

    def _stop(self, signum: int) -> None:
        if self._terminal is not None:
            self._terminal.stop(signum)
            return
        if self._process is None:
            return
        with suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(self._process.pid, signum)
            elif self._process.returncode is None:
                self._process.terminate()

    async def _run(self, process: asyncio.subprocess.Process) -> int:
        assert process.stdout is not None and process.stderr is not None
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._pump(Stream.STDOUT, process.stdout))
                tasks.create_task(self._pump(Stream.STDERR, process.stderr))
                return await process.wait()
        except BaseException:
            self._stop(signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
            raise
        finally:
            await self._close_network()

    async def _pump(self, stream: Stream, reader: asyncio.StreamReader) -> None:
        while data := await reader.read(CHUNK_SIZE):
            await self._publish(stream, data)

    async def _run_terminal(self, terminal: TerminalProcess) -> int:
        try:
            while data := await terminal.read(CHUNK_SIZE):
                await self._publish(Stream.STDOUT, data)
            return await terminal.wait()
        except BaseException:
            terminal.stop(signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
            raise
        finally:
            try:
                await self._close_network()
            finally:
                terminal.close()

    async def _publish(self, stream: Stream, data: bytes) -> None:
        endpoint: Endpoint = "output" if self._merged_output else stream
        clients = self._clients[endpoint]
        if clients:
            packet = data
            if self._merged_output:
                packet = _FRAME_HEADER.pack(_OUTPUT_STREAM_IDS[stream], len(data)) + data
            for writer in tuple(clients):
                if writer.is_closing():
                    continue
                writer.write(packet)
                if writer.transport.get_write_buffer_size() > CLIENT_BUFFER_LIMIT:
                    writer.transport.abort()
        if self._on_output is not None:
            result = self._on_output(stream, data)
            if inspect.isawaitable(result):
                await result

    def _connect(
        self, endpoint: Endpoint, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if endpoint not in self._servers:
            writer.close()
            return
        self._clients[endpoint].add(writer)
        self._connections_changed.set()
        task = asyncio.create_task(self._serve_client(endpoint, reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _serve_client(
        self, endpoint: Endpoint, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if endpoint is Stream.STDIN:
                await self._serve_input(reader)
                return
            while await reader.read(CHUNK_SIZE):
                pass
        except (OSError, ValueError):
            pass
        finally:
            self._clients[endpoint].discard(writer)
            writer.close()

    async def _serve_input(self, reader: asyncio.StreamReader) -> None:
        decoder = self._input_decoder("replace") if self._input_decoder is not None else None

        async def echo(data: bytes, *, final: bool = False) -> None:
            if decoder is not None and self._on_input_echo is not None:
                if text := decoder.decode(data, final=final):
                    result = self._on_input_echo(text)
                    if inspect.isawaitable(result):
                        await result

        if self._terminal_size is not None:
            while data := await reader.read(CHUNK_SIZE):
                await self._started.wait()
                await self.send(data)
                await echo(data)
            await echo(b"", final=True)
            return

        lines = _InputLines()
        while data := await reader.read(CHUNK_SIZE):
            await echo(data)
            for line in lines.feed(data):
                await self._started.wait()
                await self.send(line)
        await echo(b"", final=True)
        if self._close_stdin_on_eof:
            await self._started.wait()
            await self.close_stdin()

    async def _close_network(self) -> None:
        servers = tuple(self._servers.values())
        if not servers:
            return
        self._servers.clear()
        self._connections_changed.set()
        for server in servers:
            server.close()
        writers = [writer for clients in self._clients.values() for writer in clients]
        for clients in self._clients.values():
            clients.clear()
        for task in tuple(self._client_tasks):
            task.cancel()
        for writer in writers:
            writer.close()
        try:
            async with asyncio.timeout(SHUTDOWN_TIMEOUT):
                await asyncio.gather(
                    *(writer.wait_closed() for writer in writers),
                    *(server.wait_closed() for server in servers),
                    return_exceptions=True,
                )
        except TimeoutError:
            for writer in writers:
                writer.transport.abort()
