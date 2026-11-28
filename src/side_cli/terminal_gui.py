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
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from .cli import ConsoleInterrupt, show_ports
from .supervisor import INPUT_LINE_LIMIT, Ports, Stream, Supervisor

_CONPTY_UNSUPPORTED_REPLIES = ("\x1b]10;rgb:", "\x1b]11;rgb:", "\x1b]12;rgb:")
_EOF_KEYS = "Ctrl+Z, Enter" if os.name == "nt" else "Ctrl+D"
_EOF_INPUT = b"\x1a\r" if os.name == "nt" else b"\x04"


class TerminalBridge(QObject):
    output = Signal(str)
    clipboard_text = Signal(str)

    def __init__(self, supervisor: Supervisor, local_input: bool) -> None:
        super().__init__()
        self.supervisor = supervisor
        self.local_input = local_input
        self.ready = asyncio.Event()
        self.rendered = asyncio.Event()
        self.input: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(maxsize=256)
        self.error: str | None = None
        self.closed = False
        self.bracketed_paste = False

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
        if os.name == "nt" and text.startswith(_CONPTY_UNSUPPORTED_REPLIES):
            return
        self.queue_input(text.encode("utf-8"))

    @Slot(str)
    def send_binary(self, encoded: str) -> None:
        with suppress(ValueError):
            self.queue_input(base64.b64decode(encoded, validate=True))

    @property
    def accepts_input(self) -> bool:
        return self.local_input and not self.closed and self.supervisor.stdin_open

    def queue_input(self, data: bytes, *, submit: bool = False) -> bool:
        if not self.accepts_input or not (data or submit):
            return False
        try:
            self.input.put_nowait((data, submit))
        except asyncio.QueueFull:
            return False
        return True

    def queue_line(self, data: bytes) -> bool:
        if data and self.bracketed_paste:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        return self.queue_input(data, submit=True)

    @Slot(bool)
    def acknowledge_output(self, bracketed_paste: bool) -> None:
        self.bracketed_paste = bracketed_paste
        self.rendered.set()

    @Slot(str)
    def copy_text(self, text: str) -> None:
        if text:
            QApplication.clipboard().setText(text)

    @Slot()
    def paste(self) -> None:
        if self.accepts_input:
            self.clipboard_text.emit(QApplication.clipboard().text())

    async def forward_input(self) -> None:
        while True:
            data, submit = await self.input.get()
            try:
                if submit:
                    await self.supervisor.send_terminal_line(data)
                else:
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
        self.pending_line = ""
        self.setWindowTitle("side-cli — Terminal")
        self.setMinimumSize(640, 400)
        self.resize(1180, 690)
        self.process_status = QLabel("Starting terminal…")
        self.process_status.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addWidget(self.process_status, 1)

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
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.view)
        split.addWidget(self.build_sidebar())
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 1)
        split.setSizes([860, 320])
        self.setCentralWidget(split)
        self.view.load(document)

    def build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setMinimumWidth(260)
        layout = QVBoxLayout(sidebar)

        self.line_panel = QGroupBox("Standard input")
        line_layout = QVBoxLayout(self.line_panel)
        entry_row = QHBoxLayout()
        self.line_entry = QLineEdit()
        self.line_entry.setAccessibleName("Standard input")
        self.line_entry.setPlaceholderText("Type a line…")
        self.line_entry.setMaxLength(INPUT_LINE_LIMIT)
        self.line_entry.returnPressed.connect(self.submit_line)
        entry_row.addWidget(self.line_entry, 1)
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.submit_line)
        entry_row.addWidget(self.send_button)
        line_layout.addLayout(entry_row)
        options_row = QHBoxLayout()
        self.append_newline = QCheckBox("Append newline")
        self.append_newline.setChecked(True)
        options_row.addWidget(self.append_newline)
        options_row.addStretch()
        self.eof_button = QPushButton("End of File")
        self.eof_button.setToolTip(f"Send {_EOF_KEYS} to the terminal")
        self.eof_button.clicked.connect(self.submit_eof)
        options_row.addWidget(self.eof_button)
        line_layout.addLayout(options_row)
        self.input_status = QLabel(
            "Uncheck Append newline to buffer text." if self.bridge.local_input
            else "Local input is disabled; use TCP."
        )
        self.input_status.setWordWrap(True)
        line_layout.addWidget(self.input_status)
        self.line_panel.setEnabled(False)
        layout.addWidget(self.line_panel)

        ports_panel = QGroupBox("TCP ports")
        ports_layout = QFormLayout(ports_panel)
        self.port_values = {}
        for endpoint in self.bridge.supervisor.status():
            value = QLabel()
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ports_layout.addRow(endpoint.upper(), value)
            self.port_values[endpoint] = value
        layout.addWidget(ports_panel)

        activity_panel = QGroupBox("Input activity")
        activity_layout = QVBoxLayout(activity_panel)
        self.input_log = QPlainTextEdit()
        self.input_log.setReadOnly(True)
        self.input_log.setMaximumBlockCount(160)
        activity_layout.addWidget(self.input_log)
        layout.addWidget(activity_panel, 1)
        return sidebar

    def log_input(self, source: str, text: str) -> None:
        preview = repr(text[:512]) + ("…" if len(text) > 512 else "")
        self.input_log.appendPlainText(f"{source}: {preview}")

    def submit_line(self) -> None:
        if not self.bridge.accepts_input:
            return
        text = self.line_entry.text()
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in text):
            self.input_status.setText("Enter a single line without control characters.")
            return
        line = self.pending_line + text
        data = line.encode("utf-8")
        if len(data) >= INPUT_LINE_LIMIT:
            self.input_status.setText("Input line exceeds 1 MiB.")
            return
        if self.append_newline.isChecked():
            if not self.bridge.queue_line(data):
                self.input_status.setText("Terminal input is busy or closed. Try again.")
                return
            self.pending_line = ""
            self.input_status.setText("Line queued.")
            self.log_input("Line queued", line)
        elif text:
            self.pending_line = line
            self.input_status.setText(f"Buffered {len(data)} bytes until newline.")
            self.log_input("Buffered", text)
        self.line_entry.clear()

    def submit_eof(self) -> None:
        if not self.bridge.accepts_input:
            return
        if self.bridge.queue_input(_EOF_INPUT):
            self.input_status.setText(f"EOF shortcut queued ({_EOF_KEYS}).")
            self.log_input("EOF shortcut queued", _EOF_KEYS)
        else:
            self.input_status.setText("Terminal input is busy or closed. Try again.")

    def page_loaded(self, success: bool) -> None:
        if not success and not self.closed:
            self.bridge.fail("cannot load the embedded terminal")

    def renderer_stopped(self, _status, exit_code: int) -> None:
        if not self.closed:
            self.bridge.fail(f"embedded terminal renderer exited with code {exit_code}")

    def refresh(self) -> None:
        self.line_panel.setEnabled(self.bridge.accepts_input)
        for port in self.bridge.supervisor.status().values():
            address = f"{port.host}:{port.port}" if port.port else "Automatic port"
            state = "Listening" if port.listening else "Not listening"
            clients = "client" if port.clients == 1 else "clients"
            text = f"{address}\n{state} | {port.clients} {clients}"
            label = self.port_values[port.stream]
            if label.text() != text:
                label.setText(text)

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
        on_input_echo=lambda text: window.log_input("TCP", text),
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
            window.refresh()
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
