"""Read-only desktop mirror of a running terminal session."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress

from PySide6.QtCore import QObject, Property, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPlainTextEdit, QSplitter

from .events import Event, LineSource, terminal_dimensions, terminal_events
from .supervisor import HOST
from .terminal_gui import TerminalView

_EVENT_BATCH = 32


class MirrorBridge(QObject):
    updates = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()
        self.applied = asyncio.Event()

    @Property(bool, constant=True)
    def read_only(self) -> bool:
        return True

    @Slot()
    def initialized(self) -> None:
        self.ready.set()

    @Slot()
    def rendered(self) -> None:
        self.applied.set()

    @Slot(str)
    def copy_text(self, text: str) -> None:
        if text:
            QApplication.clipboard().setText(text)

    async def apply(self, updates: list) -> None:
        if updates:
            self.applied.clear()
            self.updates.emit(updates)
            await asyncio.wait_for(self.applied.wait(), 15)


class PanelWindow(QMainWindow):
    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port
        self.closed = False
        self.renderer_error: str | None = None
        self.bridge = MirrorBridge()
        self.setWindowTitle(f"side-cli — Terminal panel ({HOST}:{port})")
        self.setMinimumSize(640, 360)
        self.resize(1180, 690)
        self.status = QLabel("Connecting…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addWidget(self.status, 1)
        self.activity = QPlainTextEdit()
        self.activity.setAccessibleName("Submitted input lines")
        self.activity.setPlaceholderText("Submitted input lines appear here.")
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(160)
        self.view = TerminalView(self.bridge, self)
        self.view.loadFinished.connect(self.page_loaded)
        self.view.renderProcessTerminated.connect(self.renderer_stopped)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.view)
        split.addWidget(self.activity)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 280])
        self.setCentralWidget(split)
        self.view.load(self.view.document)

    def page_loaded(self, success: bool) -> None:
        if not success:
            self.renderer_error = "cannot load the embedded terminal"

    def renderer_stopped(self, _status, code: int) -> None:
        self.renderer_error = f"embedded terminal renderer exited with code {code}"

    def closeEvent(self, event) -> None:
        self.closed = True
        super().closeEvent(event)

    async def mirror(self) -> int:
        events: asyncio.Queue[tuple[Event, bytes] | Exception | None] = asyncio.Queue(maxsize=_EVENT_BATCH)

        async def receive() -> None:
            try:
                async for event in terminal_events(HOST, self.port):
                    await events.put(event)
            except (OSError, ValueError) as error:
                await events.put(error)
            else:
                await events.put(None)

        await asyncio.wait_for(self.bridge.ready.wait(), 15)
        receiver = asyncio.create_task(receive())
        exit_code = None
        self.status.setText("Replaying terminal…")
        try:
            while True:
                batch = [await events.get()]
                while len(batch) < _EVENT_BATCH and not events.empty():
                    batch.append(events.get_nowait())
                updates: list[str | list[int]] = []
                pending_data = bytearray()
                for item in batch:
                    if isinstance(item, tuple) and item[0] is Event.DATA:
                        pending_data.extend(item[1])
                        continue
                    if pending_data:
                        updates.append(base64.b64encode(pending_data).decode("ascii"))
                        pending_data.clear()
                    if isinstance(item, tuple) and item[0] is Event.SIZE:
                        updates.append(list(terminal_dimensions(item[1])))
                        continue
                    await self.bridge.apply(updates)
                    updates.clear()
                    if item is None:
                        if exit_code is None:
                            self.status.setText("Disconnected.")
                        return 0
                    if isinstance(item, Exception):
                        raise item
                    kind, data = item
                    if kind is Event.LINE:
                        text = data[1:].decode("utf-8")
                        preview = repr(text[:8192]) + ("…" if len(text) > 8192 else "")
                        self.activity.appendPlainText(f"{LineSource(data[0]).name}: {preview}")
                    elif kind is Event.LIVE and exit_code is None:
                        self.status.setText("Live — read only")
                    elif kind is Event.EXIT:
                        exit_code = int(data)
                        self.status.setText(f"Process exited with code {exit_code}.")
                if pending_data:
                    updates.append(base64.b64encode(pending_data).decode("ascii"))
                await self.bridge.apply(updates)
        finally:
            receiver.cancel()
            with suppress(asyncio.CancelledError):
                await receiver


async def run_terminal_panel(port: int) -> int:
    app = QApplication.instance() or QApplication(["side-cli panel"])
    app.setQuitOnLastWindowClosed(False)
    window = PanelWindow(port)
    window.show()
    session = asyncio.create_task(window.mirror())
    result = 0
    try:
        while not window.closed:
            app.processEvents()
            if window.renderer_error is not None:
                result = 1
                window.status.setText(f"side-cli: {window.renderer_error}")
                session.cancel()
            elif session.done() and not session.cancelled():
                try:
                    result = session.result()
                except (OSError, ValueError) as error:
                    result = 1
                    window.status.setText(f"side-cli: {error}")
            await asyncio.sleep(0.008)
        return result
    finally:
        session.cancel()
        await asyncio.gather(session, return_exceptions=True)
        window.close()
        app.processEvents()
