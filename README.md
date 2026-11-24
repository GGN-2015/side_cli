# side-cli

Run a command with piped standard streams and bridge them to local TCP ports.
Use the console, the Tk desktop window, or the async Python API. Receive
stdout and stderr on separate ports or on one framed output port.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run
these commands from the project directory. The project requires Python 3.11
or later and has no third-party runtime dependencies.

```shell
uv sync --locked
uv run side-cli run -- python -u -c "import sys; print('Ready'); print('Status', file=sys.stderr); print(input())"
```

Type a line in the terminal, or send it to the stdin TCP port. The example
prints the line and exits. `side_cli` and `python -m side_cli` are also entry
points. Put `run` options before `--` and the child command and its arguments
after it. Run `side-cli run --help` for supervisor options.
Commands run directly; use an explicit shell for shell syntax, for example
`-- sh -c "printf 'hello\n'"` on Linux or `-- cmd /c "echo hello"` on Windows.

```shell
uv run side-cli run --gui -- python -u -c "import sys; print('Ready'); print('Status', file=sys.stderr); print(input())"
```

The GUI shows stdout and stderr in the left-hand console. Both use the same
default colors; ANSI color sequences in each stream control their text colors.
On the right, **Send** writes the input text with an optional trailing newline.
The TCP panel shows each bound address, listening state, and active connection
count. The window keeps the output visible after the command exits. Closing
the window stops a command that is still running. GUI mode requires Tkinter
and a desktop session; run `uv run python -m tkinter` to check. If you use the
system Python on Debian or Ubuntu, install its `python3-tk` package if needed.

## TCP and process behavior

| Stream | Default address | Direction |
| --- | --- | --- |
| stdin | `127.0.0.1:8765` | TCP client to child stdin |
| stdout | `127.0.0.1:8766` | Child stdout to connected TCP clients |
| stderr | `127.0.0.1:8767` | Child stderr to connected TCP clients |

- Configure ports with `--stdin-port`, `--stdout-port`, and `--stderr-port`.
  Use `0` for an available port and `--show-ports` to print the actual addresses.
  Listeners accept unauthenticated connections from the local machine only.
- Stdin and the separate output ports carry raw bytes. Output goes to clients
  connected at the time of the write, with no replay. To capture output from
  the first byte, use `--wait-for-output-clients` and connect to each enabled
  output port before the child starts. Keep output clients connected in both
  directions until done. A slow client may be disconnected to keep other
  readers moving.
- Both modes mirror the child's stdout and stderr separately. Console mode
  also forwards terminal or piped stdin, unless you set `--no-local-stdin`.
  Terminal input, GUI input, and TCP input aren't exclusive; concurrent
  sources can interleave at chunk boundaries. Input-source EOF keeps child
  stdin open for other clients by default. Set `--close-stdin-on-eof` if the
  child should receive EOF when the first input source ends. Combine it with
  `--no-local-stdin` for commands that should read TCP input until EOF.
- Ctrl+C reaches the child as `SIGINT` on Linux. On Windows, the child shares
  the console and receives the native Ctrl+C event directly. A child that
  handles the interrupt can clean up or continue. Console mode returns the
  child's exit code, with POSIX signal exits reported as `128 + signal`.
- The child gets pipes, not a terminal. For timely output from buffered
  commands, enable their unbuffered mode (such as Python's `-u`). The GUI
  renders ANSI foreground and background colors (basic, bright, 256-color,
  and RGB), but does not emulate cursor movement or screen clearing. If a
  command disables colors when writing to a pipe, use its force-color option.
- On Windows, when launching `cmd.exe` directly, side-cli removes Clink's
  hidden tag from the child `PROMPT` but preserves the rest of the prompt.
  Clink's interactive editing and completion still require a console.

## Text encoding

`--encoding` describes the encoding used by the child process. It defaults to
`utf-8` and accepts Python text encodings such as `gb2312`, `gbk`, and `cp936`.
Choose the encoding that matches the command; the option does not change the
child's locale or code page.

```shell
uv run side-cli run --gui --encoding gb2312 -- cmd.exe
uv run side-cli panel --port 8766 --encoding gb2312
uv run side-cli input --port 8765 --encoding gb2312
```

`run` converts terminal and GUI text in both directions. `panel` decodes
output for the terminal, and `input` encodes lines typed at the terminal.
Redirected files and pipes, TCP payloads, and the byte-oriented Python API
do not transcode. Each client sets its own encoding for terminal display or
entry; the TCP protocol does not negotiate encodings.

## Single framed output port

Set `--output-port` to replace both output TCP listeners with one connection
point carrying both streams. This option cannot be combined with
`--stdout-port` or `--stderr-port`. The stdin listener, local output, and GUI
ANSI rendering keep their existing behavior. The GUI and `--show-ports`
report one combined output listener; `--wait-for-output-clients` waits for
one client on it.

```shell
uv run side-cli run --output-port 8766 --wait-for-output-clients --no-local-stdin -- python -u -c "import sys; print('Ready'); print('Status', file=sys.stderr); sys.stdout.writelines(sys.stdin)"
```

In a second terminal, connect to the framed output port:

```shell
uv run side-cli panel --port 8766
```

`panel` writes frames to its own stdout or stderr according to their stream
ID. Redirect either stream independently to capture its raw bytes. The child
still writes to the supervisor's own stdout and stderr too.

In a third terminal, send lines to the stdin port:

```shell
uv run side-cli input --port 8765
```

`input` writes nothing to stdout and reports connection errors to stderr.
It forwards complete lines, preserving line endings, and sends any final
unterminated line at local EOF. Multiple input clients can connect
at once. Each client's byte order is preserved; writes from different clients
can interleave at the supervisor's TCP read boundaries. Ctrl+C disconnects
that input client without sending a signal or an orderly EOF to the child.
An ordinary local EOF disconnects cleanly and leaves child stdin open unless
the supervisor was started with `--close-stdin-on-eof`. Use separate terminals
for the supervisor and its clients so Ctrl+C goes to the intended command.

### Frame format

The connection carries consecutive binary frames with no initial preamble:

| Field | Size | Value |
| --- | --- | --- |
| Stream ID | 1 byte | `1` = stdout, `2` = stderr |
| Payload length | 4 bytes | Unsigned big-endian byte count, 1–65,536 |
| Payload | As specified above | Raw bytes from the selected stream, including ANSI escapes |

Read five bytes for the header, then exactly the specified number of payload
bytes. Frames represent chunks, not lines, characters, or individual child
writes. Each stream retains its own byte order. Frames from the two streams
interleave in the order side-cli reads them. EOF at a frame boundary ends the
connection; EOF inside a frame indicates truncation.

## Python API

Import `Supervisor`, `Ports`, and `Stream` from `side_cli`. An async context
opens the listeners on entry and releases them on exit. `start()` launches
the child; `wait()` drains its output and returns its original exit code.

```python
import asyncio
import sys

from side_cli import Ports, Stream, Supervisor


def show_output(stream: Stream, data: bytes) -> None:
    destination = sys.stdout if stream is Stream.STDOUT else sys.stderr
    destination.buffer.write(data)
    destination.buffer.flush()


async def main() -> int:
    command = [sys.executable, "-u", "-c", "print(input())"]
    async with Supervisor(command, ports=Ports(0, 0, 0), on_output=show_output) as child:
        print(child.status()[Stream.STDIN].port, flush=True)
        await child.start()
        await child.send(b"Hello from Python\n")
        await child.close_stdin()
        return await child.wait()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

`on_output` can also be an async function. It receives raw chunks in order
within each stream; stdout and stderr may interleave. `status()` exposes port
and connection information, including when the child has not started yet.
Pass `Ports(stdin=0, output=0)` to enable framed mode with automatic ports.
In this mode, `status()["output"]` reports the combined listener, and
`on_output` still receives the original `Stream.STDOUT` or `Stream.STDERR`.
The `stdout` and `stderr` port settings apply only in separate-port mode.
Call `listen()` directly if you need to bind before starting without a context
manager, and use `aclose()` to clean up. An embedding app owns its own signal
handlers. On Windows, use the default asyncio Proactor event loop.

### TCP clients

Import `PanelClient` and `InputClient` from `side_cli`. Both connect to the
local supervisor as async context managers. `PanelClient` yields
`(Stream.STDOUT, bytes)` or `(Stream.STDERR, bytes)` pairs until the connection
ends; malformed or truncated frames raise `ValueError`. `InputClient.send()`
writes bytes unchanged, and `send_line()` appends an LF if one is missing.
Context cancellation aborts the input connection rather than sending an
orderly EOF. Use `close(abort=True)` for an explicit abort.

```python
import sys

from side_cli import InputClient, PanelClient, Stream


async def receive_output(port: int = 8766) -> None:
    async with PanelClient(port) as panel:
        async for stream, data in panel:
            destination = sys.stdout if stream is Stream.STDOUT else sys.stderr
            destination.buffer.write(data)
            destination.buffer.flush()


async def send_input(port: int = 8765) -> None:
    async with InputClient(port) as client:
        await client.send_line(b"Hello from Python")
```

## Build

```shell
uv build
```

Released under the [MIT License](LICENSE).
