"""Qt desktop window for the interactive PTY."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from .cli import ConsoleInterrupt, show_ports
from .supervisor import Ports, Stream, Supervisor


class TerminalBridge(QObject):
    output = Signal(str)
    clipboard_text = Signal(str)

    def __init__(self, supervisor: Supervisor, local_input: bool) -> None:
        super().__init__()
        self.supervisor = supervisor
        self.local_input = local_input
        self.ready = asyncio.Event()
        self.rendered = asyncio.Event()
        self.input: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.error: str | None = None
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.rendered.set()

    def fail(self, message: str) -> None:
        self.error = message
        self.ready.set()
        self.close()

    @Slot(int, int)
    def resize_terminal(self, columns: int, rows: int) -> None:
        if self.closed or not (2 <= columns <= 4096 and 1 <= rows <= 4096):
            return
        try:
            self.supervisor.resize_terminal(columns, rows)
        except OSError as error:
            self.fail(f"cannot resize the terminal: {error}")
        else:
            self.ready.set()

    @Slot(str)
    def send_text(self, text: str) -> None:
        if self.local_input and not self.closed and self.supervisor.stdin_open:
            self.queue_input(text.encode("utf-8"))

    @Slot(str)
    def send_binary(self, encoded: str) -> None:
        if self.local_input and not self.closed and self.supervisor.stdin_open:
            with suppress(ValueError):
                self.queue_input(base64.b64decode(encoded, validate=True))

    def queue_input(self, data: bytes) -> None:
        if data:
            with suppress(asyncio.QueueFull):
                self.input.put_nowait(data)

    @Slot()
    def acknowledge_output(self) -> None:
        self.rendered.set()

    @Slot(str)
    def copy_text(self, text: str) -> None:
        if text:
            QApplication.clipboard().setText(text)

    @Slot()
    def paste(self) -> None:
        if self.local_input and self.supervisor.stdin_open:
            self.clipboard_text.emit(QApplication.clipboard().text())

    async def forward_input(self) -> None:
        while True:
            data = await self.input.get()
            try:
                await self.supervisor.send(data)
            except OSError as error:
                if self.supervisor.stdin_open:
                    self.fail(f"cannot write to the terminal: {error}")
                    return

    async def write(self, _stream: Stream, data: bytes) -> None:
        if not self.closed:
            self.rendered.clear()
            self.output.emit(base64.b64encode(data).decode("ascii"))
            await self.rendered.wait()


class TerminalPage(QWebEnginePage):
    def __init__(self, document: QUrl, parent: QWebEngineView) -> None:
        super().__init__(parent)
        self.document = document

    def acceptNavigationRequest(self, url: QUrl, _kind, main_frame: bool) -> bool:
        return main_frame and url == self.document


class TerminalWindow(QMainWindow):
    def __init__(self, bridge: TerminalBridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.closed = False
        self.setWindowTitle("side-cli — Terminal")
        self.setMinimumSize(500, 320)
        self.resize(1080, 690)
        self.process_status = QLabel("Starting terminal…")
        self.port_status = QLabel()
        self.statusBar().addWidget(self.process_status, 1)
        self.statusBar().addPermanentWidget(self.port_status)

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        document = QUrl.fromLocalFile(str(Path(__file__).with_name("terminal_assets") / "index.html"))
        page = TerminalPage(document, self.view)
        channel = QWebChannel(page)
        channel.registerObject("bridge", bridge)
        page.setWebChannel(channel)
        self.view.setPage(page)
        self.view.loadFinished.connect(self.page_loaded)
        self.view.renderProcessTerminated.connect(self.renderer_stopped)
        self.setCentralWidget(self.view)
        self.view.load(document)

    def page_loaded(self, success: bool) -> None:
        if not success and not self.closed:
            self.bridge.fail("cannot load the embedded terminal")

    def renderer_stopped(self, _status, exit_code: int) -> None:
        if not self.closed:
            self.bridge.fail(f"embedded terminal renderer exited with code {exit_code}")

    def show_ports(self) -> None:
        listening = [
            f"{port.stream}: {port.host}:{port.port} ({port.clients})"
            for port in self.bridge.supervisor.status().values() if port.listening
        ]
        self.port_status.setText("   ".join(listening))

    def closeEvent(self, event) -> None:
        self.closed = True
        self.bridge.close()
        super().closeEvent(event)

    async def run_command(self, args: argparse.Namespace) -> int:
        try:
            with ConsoleInterrupt(self.bridge.supervisor) as interrupts:
                try:
                    await asyncio.wait_for(self.bridge.ready.wait(), 15)
                except TimeoutError:
                    self.bridge.fail("embedded terminal did not start; check the Qt WebEngine installation")
                if self.bridge.error is not None:
                    raise RuntimeError(self.bridge.error)
                async with self.bridge.supervisor as supervisor:
                    if args.show_ports:
                        show_ports(supervisor)
                    if args.wait_for_output_clients:
                        self.process_status.setText("Waiting for a TCP output client…")
                    if not await interrupts.launch(args.wait_for_output_clients):
                        self.process_status.setText("Launch interrupted.")
                        return 130
                    self.process_status.setText(f"Running (PID {supervisor.pid})")
                    code = await supervisor.wait()
                    if self.bridge.error is not None:
                        raise RuntimeError(self.bridge.error)
                    self.process_status.setText(f"Process exited with code {code}.")
                    return code
        except (OSError, RuntimeError, ValueError) as error:
            self.process_status.setText(f"side-cli: {error}")
            return 1


async def run_terminal_gui(args: argparse.Namespace, ports: Ports) -> int:
    app = QApplication.instance() or QApplication(["side-cli"])
    supervisor = Supervisor(
        args.command, ports=ports, on_output=lambda stream, data: bridge.write(stream, data),
        terminal_size=(80, 24),
        env=os.environ | {"TERM": "xterm-256color", "COLORTERM": "truecolor"},
    )
    bridge = TerminalBridge(supervisor, local_input=not args.no_local_stdin)
    window = TerminalWindow(bridge)
    window.show()
    window.view.setFocus()
    command = asyncio.create_task(window.run_command(args))
    keyboard = asyncio.create_task(bridge.forward_input())
    try:
        while not window.closed:
            app.processEvents()
            window.show_ports()
            if bridge.error is not None:
                window.process_status.setText(f"side-cli: {bridge.error}")
                if not command.done() and not command.cancelling():
                    command.cancel()
            await asyncio.sleep(0.012)
        if bridge.error is not None:
            return 1
        return command.result() if command.done() else 0
    finally:
        bridge.close()
        keyboard.cancel()
        command.cancel()
        await asyncio.gather(command, keyboard, return_exceptions=True)
        window.close()
        app.processEvents()
