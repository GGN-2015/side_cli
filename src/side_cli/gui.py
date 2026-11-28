"""Tk desktop frontend for side-cli."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import tkinter as tk
from contextlib import suppress
from io import IncrementalNewlineDecoder
from time import monotonic
from tkinter import ttk

from .ansi import AnsiColors, ColorRun
from .cli import ConsoleInterrupt, ConsoleOutput, _visible_input, show_ports
from .supervisor import INPUT_LINE_LIMIT, Ports, Stream, Supervisor


class Window:
    def __init__(
        self, root: tk.Tk, encoding: str, supervisor: Supervisor, console: ConsoleOutput | None = None,
    ) -> None:
        self.root = root
        self.encoding = encoding
        self.supervisor = supervisor
        self.console = console
        self.closed = False
        self.send_task: asyncio.Task[None] | None = None
        self._input_pending = ""
        self._input_encoder = codecs.getincrementalencoder(encoding)()
        self.decoders = {
            stream: IncrementalNewlineDecoder(
                codecs.getincrementaldecoder(encoding)("replace"), translate=True
            )
            for stream in (Stream.STDOUT, Stream.STDERR)
        }
        self.colors = {stream: AnsiColors() for stream in self.decoders}
        self.pending: list[ColorRun] = []
        self._color_tags: set[str] = set()
        self.process_status = tk.StringVar(value="Starting listeners...")
        self.input_status = tk.StringVar()
        self.add_newline = tk.BooleanVar(value=True)
        self.port_values = {
            endpoint: tk.StringVar(value="Not listening") for endpoint in supervisor.status()
        }

        root.title("side-cli")
        root.geometry("1040x590")
        root.minsize(640, 360)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panels = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
        panels.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        console = ttk.Frame(panels, padding=(0, 0, 8, 0))
        sidebar = ttk.Frame(panels, padding=(8, 0, 0, 0), width=290)
        panels.add(console, weight=3)
        panels.add(sidebar, weight=1)

        ttk.Label(console, text="Process output", font=("TkDefaultFont", 12, "bold")).pack(
            anchor=tk.W, pady=(0, 6)
        )
        terminal = ttk.Frame(console)
        terminal.pack(fill=tk.BOTH, expand=True)
        self.output = tk.Text(
            terminal, state=tk.DISABLED, wrap=tk.CHAR, font="TkFixedFont",
            background="#151920", foreground="#e5e9f0", padx=10, pady=8,
            selectbackground="#394f70", relief=tk.FLAT, borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(terminal, orient=tk.VERTICAL, command=self.output.yview)
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._caret = tk.Frame(self.output, background=self.output.cget("foreground"), takefocus=False)
        self._caret_position: tuple[int, int, int] | None = None
        self._caret_epoch = monotonic()

        input_panel = ttk.LabelFrame(sidebar, text="Standard input", padding=10)
        input_panel.pack(fill=tk.X)
        input_panel.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(input_panel)
        self.entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.entry.bind("<Return>", self.submit)
        self.output.bind("<ButtonRelease-1>", self._focus_after_output_click)
        self._caret.bind("<Button-1>", self._focus_after_output_click)
        self.send_button = ttk.Button(input_panel, text="Send", command=self.submit, state=tk.DISABLED)
        self.send_button.grid(row=0, column=1)
        ttk.Checkbutton(input_panel, text="Append newline", variable=self.add_newline).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.eof_button = ttk.Button(input_panel, text="End of File", command=self.submit_eof, state=tk.DISABLED)
        self.eof_button.grid(row=1, column=1, sticky="e", pady=(8, 0))
        ttk.Label(input_panel, textvariable=self.input_status, wraplength=245).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )

        ports_panel = ttk.LabelFrame(sidebar, text="TCP ports", padding=10)
        ports_panel.pack(fill=tk.X, pady=(12, 0))
        for row, (endpoint, value) in enumerate(self.port_values.items()):
            label = "STDOUT / STDERR" if endpoint == "output" else endpoint.upper()
            ttk.Label(ports_panel, text=label).grid(
                row=row, column=0, sticky="nw", padx=(0, 14), pady=(0, 8)
            )
            ttk.Label(ports_panel, textvariable=value).grid(
                row=row, column=1, sticky="w", pady=(0, 8)
            )
        ttk.Label(sidebar, textvariable=self.process_status, wraplength=270).pack(
            anchor=tk.W, pady=(12, 0)
        )
        self.entry.focus_set()

    def _focus_after_output_click(self, event: tk.Event) -> None:
        if event.widget is self.output and self.output.tag_ranges(tk.SEL):
            self.output.focus_set()
        else:
            self.entry.focus_set()

    def close(self) -> None:
        self.closed = True
        self.root.withdraw()

    def receive(self, stream: Stream, data: bytes) -> None:
        self.pending.extend(self.colors[stream].feed(self.decoders[stream].decode(data)))

    async def echo_input(self, text: str) -> None:
        if visible := _visible_input(text):
            self.pending.append((visible, (None, None)))
        if self.console is not None:
            await self.console.echo_input(text)

    def finish_output(self) -> None:
        for stream, decoder in self.decoders.items():
            self.pending.extend(self.colors[stream].feed(decoder.decode(b"", final=True), final=True))

    def _color_tag(self, option: str, color: str) -> str:
        tag = f"ansi-{option}-{color}"
        if tag not in self._color_tags:
            self.output.tag_configure(tag, **{option: color})
            self._color_tags.add(tag)
        return tag

    def _refresh_caret(self) -> None:
        position = None
        if (monotonic() - self._caret_epoch) % 1.0 < 0.5:
            if bounds := self.output.bbox("end-1c"):
                left, top, _, height = bounds
                position = left, top, height
        if position == self._caret_position:
            return
        self._caret_position = position
        if position is None:
            self._caret.place_forget()
        else:
            left, top, height = position
            self._caret.place(x=left, y=top, width=2, height=height, bordermode="outside")

    def refresh(self) -> None:
        if self.pending:
            follow = self.output.yview()[1] >= 0.999
            self.output.configure(state=tk.NORMAL)
            for text, (foreground, background) in self.pending:
                tags = tuple(
                    self._color_tag(option, color)
                    for option, color in (("foreground", foreground), ("background", background))
                    if color is not None
                )
                self.output.insert(tk.END, text, tags)
            self.output.configure(state=tk.DISABLED)
            self.pending.clear()
            if follow:
                self.output.see(tk.END)
            self._caret_epoch = monotonic()
        self._refresh_caret()
        for port in self.supervisor.status().values():
            state = "Listening" if port.listening else "Not listening"
            clients = "client" if port.clients == 1 else "clients"
            address = f"{port.host}:{port.port}" if port.port else "Automatic port"
            value = f"{address}\n{state} | {port.clients} {clients}"
            if self.port_values[port.stream].get() != value:
                self.port_values[port.stream].set(value)
        input_state = tk.NORMAL if self.supervisor.stdin_open and self.send_task is None else tk.DISABLED
        self.send_button.configure(state=input_state)
        self.eof_button.configure(state=input_state)

    def submit(self, *_: object) -> str:
        if self.send_task is None and self.supervisor.stdin_open:
            text = self.entry.get()
            payload = text + ("\n" if self.add_newline.get() else "")
            if payload:
                self.send_task = asyncio.create_task(self.send(text, payload))
        return "break"

    async def send(self, text: str, payload: str) -> None:
        try:
            *lines, tail = (self._input_pending + payload).split("\n")
            if len(tail.encode(self.encoding)) >= INPUT_LINE_LIMIT or any(
                len((line + "\n").encode(self.encoding)) > INPUT_LINE_LIMIT for line in lines
            ):
                raise ValueError("input line exceeds 1 MiB")
            await self.echo_input(payload)
            for line in lines:
                await self.supervisor.send(self._input_encoder.encode(line + "\n"))
            self._input_pending = tail
        except (OSError, ValueError) as error:
            self.input_status.set(f"Send failed: {error}")
        else:
            if self.entry.get() == text:
                self.entry.delete(0, tk.END)
            self.input_status.set("Buffered until newline." if tail else "")
        finally:
            self.send_task = None

    def submit_eof(self) -> None:
        if self.send_task is None and self.supervisor.stdin_open:
            self.send_task = asyncio.create_task(self.send_eof())

    async def send_eof(self) -> None:
        try:
            final_data = self._input_encoder.encode(self._input_pending, final=True) if self._input_pending else b""
            await self.supervisor.close_stdin(final_data=final_data)
        except (OSError, ValueError) as error:
            self.input_status.set(f"EOF failed: {error}")
        else:
            self._input_pending = ""
            self.input_status.set("EOF sent. Standard input is closed.")
        finally:
            self.send_task = None

    async def run_command(self, args: argparse.Namespace) -> int:
        try:
            with ConsoleInterrupt(self.supervisor) as interrupts:
                async with self.supervisor:
                    if args.show_ports:
                        show_ports(self.supervisor)
                    if args.wait_for_output_clients:
                        if "output" in self.port_values:
                            self.process_status.set("Waiting for an output client...")
                        else:
                            self.process_status.set("Waiting for stdout and stderr clients...")
                    if not await interrupts.launch(args.wait_for_output_clients):
                        self.process_status.set("Launch interrupted.")
                        return 130
                    self.process_status.set(f"Running (PID {self.supervisor.pid})")
                    code = await self.supervisor.wait()
                    self.process_status.set(f"Process exited with code {code}.")
                    return code
        except (OSError, RuntimeError, ValueError) as error:
            self.process_status.set(f"side-cli: {error}")
            return 1
        finally:
            self.finish_output()


async def run_gui(args: argparse.Namespace, ports: Ports) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as error:
        raise RuntimeError(f"cannot open the Tk desktop window: {error}") from error
    console = ConsoleOutput(args.encoding)

    async def receive(stream: Stream, data: bytes) -> None:
        window.receive(stream, data)
        await console.write(stream, data)

    supervisor = Supervisor(
        args.command, ports=ports, on_output=receive,
        on_input_echo=lambda text: window.echo_input(text), input_encoding=args.encoding,
        close_stdin_on_eof=args.close_stdin_on_eof,
    )
    window = Window(root, args.encoding, supervisor, console)
    command = asyncio.create_task(window.run_command(args))
    try:
        while not window.closed:
            window.refresh()
            root.update()
            await asyncio.sleep(0.025)
        return command.result() if command.done() else 0
    finally:
        if window.send_task is not None:
            window.send_task.cancel()
            with suppress(asyncio.CancelledError):
                await window.send_task
        command.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await command
            await console.finish()
        finally:
            root.destroy()
