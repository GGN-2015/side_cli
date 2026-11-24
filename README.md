# side-cli

Run a command with piped standard streams and bridge them to three local TCP
ports. Use the console, the Tk desktop window, or the async Python API.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run
these commands from the project directory. The project requires Python 3.11
or later and has no third-party runtime dependencies.

```shell
uv sync --locked
uv run side-cli -- python -u -c "import sys; print('Ready'); print('Status', file=sys.stderr); print(input())"
```

Type a line in the terminal, or send it to the stdin TCP port. The example
prints the line and exits. `side_cli` and `python -m side_cli` are also entry
points. Put side-cli options before `--` and the child's arguments after it.
Commands run directly; use an explicit shell for shell syntax, for example
`-- sh -c "printf 'hello\n'"` on Linux or `-- cmd /c "echo hello"` on Windows.

```shell
uv run side-cli --gui -- python -u -c "import sys; print('Ready'); print('Status', file=sys.stderr); print(input())"
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
- The TCP streams carry raw bytes. Output goes to clients connected at the
  time of the write, with no replay. To capture output from the first byte,
  use `--wait-for-output-clients` and connect to both output ports before the
  child starts. Keep output clients connected in both directions until done.
  A slow client may be disconnected to keep other readers moving.
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
  The GUI uses UTF-8 by default; `--encoding` changes GUI input/output and
  Windows console output decoding.

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
Call `listen()` directly if you need to bind before starting without a context
manager, and use `aclose()` to clean up. An embedding app owns its own signal
handlers. On Windows, use the default asyncio Proactor event loop.

## Build

```shell
uv build
```

Released under the [MIT License](LICENSE).
