"use strict";

new QWebChannel(qt.webChannelTransport, ({ objects: { bridge } }) => {
  const element = document.getElementById("terminal");
  const terminal = new Terminal({
    cursorBlink: true,
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
  terminal.loadAddon(layout);
  terminal.attachCustomKeyEventHandler(event => {
    if (!event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey) return true;
    const key = event.key.toLowerCase();
    if (key !== "c" && key !== "v") return true;
    if (event.type === "keydown") {
      event.preventDefault();
      if (key === "c") bridge.copy_text(terminal.getSelection());
      else bridge.paste();
    }
    return false;
  });
  terminal.onData(text => bridge.send_text(text));
  terminal.onBinary(text => bridge.send_binary(btoa(text)));
  terminal.onResize(({ cols, rows }) => bridge.resize_terminal(cols, rows));
  bridge.clipboard_text.connect(text => terminal.paste(text));
  bridge.output.connect(encoded => {
    const bytes = Uint8Array.from(atob(encoded), character => character.charCodeAt(0));
    terminal.write(bytes, () => bridge.acknowledge_output(terminal.modes.bracketedPasteMode));
  });
  terminal.open(element);
  layout.fit();
  bridge.resize_terminal(terminal.cols, terminal.rows);
  new ResizeObserver(() => layout.fit()).observe(element);
  terminal.focus();
});
