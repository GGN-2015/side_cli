"use strict";

new QWebChannel(qt.webChannelTransport, ({ objects: { bridge } }) => {
  const element = document.getElementById("terminal");
  const readOnly = bridge.read_only;
  const terminal = new Terminal({
    cursorBlink: true,
    disableStdin: readOnly,
    scrollback: 5000,
    fontFamily: '"Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace',
    fontSize: 15,
    theme: {
      background: "#151920",
      foreground: "#e5e9f0",
      cursor: "#e5e9f0",
      selectionBackground: "#394f70",
    },
  });
  const layout = new FitAddon.FitAddon();
  terminal.attachCustomKeyEventHandler(event => {
    if (!event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey) return true;
    const key = event.key.toLowerCase();
    if (key !== "c" && key !== "v") return true;
    if (event.type === "keydown") {
      event.preventDefault();
      if (key === "c") bridge.copy_text(terminal.getSelection());
      else if (!readOnly) bridge.paste();
    }
    return false;
  });
  const write = encoded => new Promise(resolve => {
    const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
    terminal.write(bytes, resolve);
  });
  terminal.open(element);
  if (readOnly) {
    document.body.classList.add("mirror");
    element.setAttribute("aria-label", "Read-only terminal mirror");
    bridge.updates.connect(async updates => {
      for (const update of updates) {
        if (typeof update === "string") {
          await write(update);
        } else {
          terminal.resize(...update);
          const screen = element.querySelector(".xterm-screen");
          element.style.width = `${screen.clientWidth + 16}px`;
          element.style.height = `${screen.clientHeight}px`;
        }
      }
      bridge.rendered();
    });
    bridge.initialized();
    terminal.focus();
    return;
  }
  terminal.loadAddon(layout);
  terminal.onData(text => bridge.send_text(text));
  terminal.onBinary(text => bridge.send_binary(btoa(text)));
  terminal.onResize(({ cols, rows }) => bridge.resize_terminal(cols, rows));
  bridge.clipboard_text.connect(text => terminal.paste(text));
  bridge.output.connect(async encoded => {
    await write(encoded);
    bridge.acknowledge_output(terminal.modes.bracketedPasteMode);
  });
  layout.fit();
  bridge.resize_terminal(terminal.cols, terminal.rows);
  new ResizeObserver(() => layout.fit()).observe(element);
  terminal.focus();
});
