"""Console entry point and frontend lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import os
import signal
import sys
import threading
from concurrent.futures import CancelledError
from contextlib import suppress

from .clients import InputClient, PanelClient
from .supervisor import CHUNK_SIZE, Ports, Stream, Supervisor


class ConsoleInterrupt:
    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        self.before_start = False

    def __enter__(self) -> ConsoleInterrupt:
        self.previous = signal.getsignal(signal.SIGINT)
        if os.name == "posix":
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self._receive)
        else:
            signal.signal(signal.SIGINT, self._receive)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if os.name == "posix":
            asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, self.previous)

    def _receive(self, *_: object) -> None:
        if self.supervisor.pid is None:
            self.before_start = True
        else:
            self.supervisor.forward_console_interrupt()

    async def launch(self, wait_for_clients: bool) -> bool:
        if wait_for_clients:
            ready = asyncio.create_task(self.supervisor.wait_for_output_clients())
            try:
                while not ready.done() and not self.before_start:
                    await asyncio.wait((ready,), timeout=0.1)
                if ready.done():
                    ready.result()
            finally:
                ready.cancel()
                with suppress(asyncio.CancelledError):
                    await ready
        if self.before_start:
            return False
        await self.supervisor.start()
        if self.before_start:
            if os.name == "posix":
                self.supervisor.forward_console_interrupt()
            else:
                self.supervisor.terminate()
        return True


def _start_local_input(supervisor: Supervisor, close_on_eof: bool) -> threading.Event:
    loop = asyncio.get_running_loop()
    stopped = threading.Event()
    binary = getattr(sys.stdin, "buffer", None)
    if binary is None:
        return stopped
    raw = getattr(binary, "raw", binary)

    def read_stdin() -> None:
        while not stopped.is_set():
            try:
                data = raw.read(CHUNK_SIZE)
                if stopped.is_set() or data is None:
                    return
                if not data and not close_on_eof:
                    return
                operation = supervisor.send(data) if data else supervisor.close_stdin()
                try:
                    future = asyncio.run_coroutine_threadsafe(operation, loop)
                except RuntimeError:
                    operation.close()
                    return
                future.result()
                if not data:
                    return
            except (OSError, RuntimeError, CancelledError):
                return

    threading.Thread(target=read_stdin, name="side-cli-stdin", daemon=True).start()
    return stopped


class ConsoleOutput:
    def __init__(self, encoding: str) -> None:
        self.sinks = {}
        self.decoders = {}
        for stream, file in ((Stream.STDOUT, sys.stdout), (Stream.STDERR, sys.stderr)):
            binary = getattr(file, "buffer", None)
            if binary is not None:
                self.sinks[stream] = getattr(binary, "raw", binary)
                if os.name == "nt" and type(self.sinks[stream]).__name__ == "_WindowsConsoleIO":
                    self.decoders[stream] = codecs.getincrementaldecoder(encoding)("replace")

    async def write(self, stream: Stream, data: bytes, *, final: bool = False) -> None:
        if stream not in self.sinks:
            return
        if stream in self.decoders:
            data = self.decoders[stream].decode(data, final=final).encode("utf-8")
        try:
            await asyncio.to_thread(self._write_all, stream, data)
        except OSError:
            del self.sinks[stream]

    def _write_all(self, stream: Stream, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = self.sinks[stream].write(remaining)
            if not written:
                raise OSError("console output is not writable")
            remaining = remaining[written:]

    async def finish(self) -> None:
        for stream in self.decoders:
            await self.write(stream, b"", final=True)


async def _run_panel(port: int, encoding: str) -> int:
    output = ConsoleOutput(encoding)
    async with PanelClient(port) as client:
        try:
            async for stream, data in client:
                await output.write(stream, data)
        finally:
            await output.finish()
    return 0


async def _send_stdin_lines(client: InputClient) -> int:
    binary = getattr(sys.stdin, "buffer", None)
    if binary is None:
        return 0
    raw = getattr(binary, "raw", binary)
    loop = asyncio.get_running_loop()
    stopped = threading.Event()
    chunks: asyncio.Queue[bytes | OSError] = asyncio.Queue(maxsize=1)

    def read_stdin() -> None:
        while not stopped.is_set():
            try:
                item: bytes | OSError = raw.read(CHUNK_SIZE) or b""
            except OSError as error:
                item = error
            if stopped.is_set():
                return
            operation = chunks.put(item)
            try:
                future = asyncio.run_coroutine_threadsafe(operation, loop)
            except RuntimeError:
                operation.close()
                return
            try:
                future.result()
            except CancelledError:
                return
            if not item or isinstance(item, OSError):
                return

    threading.Thread(target=read_stdin, name="side-cli-input", daemon=True).start()
    pending = bytearray()
    try:
        while item := await chunks.get():
            if isinstance(item, OSError):
                return 1
            lines = item.split(b"\n")
            pending.extend(lines[0])
            if len(lines) > 1:
                await client.send_line(bytes(pending))
                for line in lines[1:-1]:
                    await client.send_line(line)
                pending.clear()
                pending.extend(lines[-1])
        if pending:
            await client.send(bytes(pending))
        return 0
    finally:
        stopped.set()


async def _run_input(port: int) -> int:
    async with InputClient(port) as client:
        sending = asyncio.create_task(_send_stdin_lines(client))
        disconnected = asyncio.create_task(client.wait_for_disconnect())
        try:
            done, _ = await asyncio.wait((sending, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                disconnected.result()
            return sending.result() if sending in done else 0
        finally:
            sending.cancel()
            disconnected.cancel()
            await asyncio.gather(sending, disconnected, return_exceptions=True)


def show_ports(supervisor: Supervisor) -> None:
    for port in supervisor.status().values():
        print(f"side-cli: {port.stream} {port.host}:{port.port}", file=sys.stderr, flush=True)


async def _run_console(args: argparse.Namespace, ports: Ports) -> int:
    output = ConsoleOutput(args.encoding)
    supervisor = Supervisor(
        args.command, ports=ports, on_output=output.write,
        close_stdin_on_eof=args.close_stdin_on_eof,
    )
    with ConsoleInterrupt(supervisor) as interrupts:
        async with supervisor:
            if args.show_ports:
                show_ports(supervisor)
            if not await interrupts.launch(args.wait_for_output_clients):
                return 130
            local_input = None
            try:
                if not args.no_local_stdin:
                    local_input = _start_local_input(supervisor, args.close_stdin_on_eof)
                return await supervisor.wait()
            finally:
                if local_input is not None:
                    local_input.set()
                await output.finish()


def _encoding(value: str) -> str:
    try:
        decoder = codecs.getincrementaldecoder(value)
        if not isinstance(decoder("replace").decode(b""), str):
            raise ValueError(f"{value!r} is not a text encoding")
        "".encode(value)
    except (ValueError, LookupError, TypeError) as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def _client_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535")
    return port


def _configure_run(parser: argparse.ArgumentParser, defaults: Ports) -> None:
    parser.add_argument("--gui", action="store_true", help="open the desktop interface")
    parser.add_argument("--stdin-port", type=int, default=defaults.stdin,
                        help="TCP port for stdin (default: %(default)s; 0: auto)")
    for stream in (Stream.STDOUT, Stream.STDERR):
        parser.add_argument(f"--{stream}-port", type=int,
                            help=f"TCP port for {stream} (default: {getattr(defaults, stream)}; 0: auto)")
    parser.add_argument("--output-port", type=int,
                        help="one TCP port for framed stdout and stderr (replaces both output ports; 0: auto)")
    parser.add_argument("--show-ports", action="store_true", help="print bound ports to stderr")
    parser.add_argument("--wait-for-output-clients", action="store_true",
                        help="wait for a client on each output port before launching")
    parser.add_argument("--no-local-stdin", action="store_true",
                        help="read stdin only from TCP in console mode")
    parser.add_argument("--close-stdin-on-eof", action="store_true",
                        help="close child stdin on EOF from a TCP or local input source")
    parser.add_argument("--encoding", type=_encoding, default="utf-8",
                        help="text encoding for GUI and Windows console output (default: utf-8)")
    parser.add_argument("command", nargs=argparse.REMAINDER)


def _parse_run_ports(parser: argparse.ArgumentParser, args: argparse.Namespace, defaults: Ports) -> Ports:
    if args.command and args.command[0] == "--":
        del args.command[0]
    if not args.command or not args.command[0]:
        parser.error("a command is required")
    if args.output_port is not None and (args.stdout_port is not None or args.stderr_port is not None):
        parser.error("--output-port cannot be combined with --stdout-port or --stderr-port")
    try:
        return Ports(
            stdin=args.stdin_port,
            stdout=defaults.stdout if args.stdout_port is None else args.stdout_port,
            stderr=defaults.stderr if args.stderr_port is None else args.stderr_port,
            output=args.output_port,
        )
    except ValueError as error:
        parser.error(str(error))


def main(argv: list[str] | None = None) -> int:
    defaults = Ports()
    parser = argparse.ArgumentParser(
        prog="side-cli", description="Bridge a command's standard streams over local TCP."
    )
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="run and supervise a command",
                              usage="%(prog)s [options] -- command [arguments ...]")
    _configure_run(run, defaults)
    panel = commands.add_parser("panel", help="mirror stdout and stderr from a framed output port")
    panel.add_argument("--port", type=_client_port, default=defaults.stdout,
                       help="framed output port (default: %(default)s)")
    panel.add_argument("--encoding", type=_encoding, default="utf-8",
                       help="text encoding for Windows console output (default: %(default)s)")
    input_parser = commands.add_parser("input", help="send local stdin lines to a running command")
    input_parser.add_argument("--port", type=_client_port, default=defaults.stdin,
                              help="stdin port (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        if args.action == "panel":
            return asyncio.run(_run_panel(args.port, args.encoding))
        if args.action == "input":
            return asyncio.run(_run_input(args.port))
        ports = _parse_run_ports(run, args, defaults)
        if args.gui:
            try:
                from .gui import run_gui
            except ImportError as error:
                raise RuntimeError("GUI mode requires Tkinter; try 'python -m tkinter'") from error

            code = asyncio.run(run_gui(args, ports))
        else:
            code = asyncio.run(_run_console(args, ports))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"side-cli: {error}", file=sys.stderr)
        return 1
    return 128 - code if code < 0 else code
