"""Incremental ANSI color decoding for the GUI text viewer."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from itertools import islice

Colors = tuple[str | None, str | None]
ColorRun = tuple[str, Colors]

_CONTROL = re.compile(r"[\x1b\x90\x98\x9b\x9d\x9e\x9f]")
_ESCAPE = re.compile(r"[ -/]*[0-~]")
_ESCAPE_PREFIX = re.compile(r"[ -/]*")
_CSI = re.compile(r"([0-?]*)([ -/]*)([@-~])")
_CSI_PREFIX = re.compile(r"[0-?]*[ -/]*")
_SGR_PARAMETERS = re.compile(r"[0-9:;]*")
_STRING_END = re.compile(r"\x1b\\|\x9c")
_OSC_END = re.compile(r"\x1b\\|\x9c|\x07")
_MAX_PARAMETERS = 128

_PALETTE = (
    "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
)
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def _indexed_color(index: int) -> str:
    if index < 16:
        return _PALETTE[index]
    if index >= 232:
        gray = 8 + 10 * (index - 232)
        return f"#{gray:02x}{gray:02x}{gray:02x}"
    red, remainder = divmod(index - 16, 36)
    green, blue = divmod(remainder, 6)
    return f"#{_CUBE_LEVELS[red]:02x}{_CUBE_LEVELS[green]:02x}{_CUBE_LEVELS[blue]:02x}"


def _extended_color(mode: str, params: Sequence[str]) -> str | None:
    if any(not param.isdecimal() for param in params):
        return None
    values = [int(param) for param in params]
    if any(value > 255 for value in values):
        return None
    if mode == "5" and len(values) == 1:
        return _indexed_color(values[0])
    if mode == "2" and len(values) == 3:
        return "#" + "".join(f"{value:02x}" for value in values)
    return None


class AnsiColors:
    """Convert one stream's chunks to visible text and foreground/background pairs."""

    def __init__(self) -> None:
        self._foreground: str | None = None
        self._background: str | None = None
        self._partial = ""
        self._string_end: re.Pattern[str] | None = None

    def feed(self, text: str, *, final: bool = False) -> Iterator[ColorRun]:
        text = self._partial + text
        self._partial = ""
        offset = 0
        while offset < len(text):
            if self._string_end is not None:
                string_end = self._string_end.search(text, offset)
                if string_end is None:
                    if not final and text.endswith("\x1b"):
                        self._partial = "\x1b"
                    return
                self._string_end = None
                offset = string_end.end()
                continue

            control = _CONTROL.search(text, offset)
            end = control.start() if control else len(text)
            if offset < end:
                yield text[offset:end], (self._foreground, self._background)
            if control is None:
                return

            offset = control.end()
            if control[0] == "\x1b":
                kind = text[offset:offset + 1]
                if kind == "[":
                    offset += 1
                elif kind and kind in "]PX^_":
                    self._string_end = _OSC_END if kind == "]" else _STRING_END
                    offset += 1
                    continue
                else:
                    escape = _ESCAPE.match(text, offset)
                    if escape is not None:
                        offset = escape.end()
                    elif _ESCAPE_PREFIX.fullmatch(text, offset):
                        if not final and len(text) - offset <= _MAX_PARAMETERS:
                            self._partial = text[end:]
                        return
                    continue
            elif control[0] != "\x9b":
                self._string_end = _OSC_END if control[0] == "\x9d" else _STRING_END
                continue

            csi = _CSI.match(text, offset)
            if csi is not None:
                params, intermediate, command = csi.groups()
                if command == "m" and not intermediate:
                    self._set_colors(params)
                offset = csi.end()
            elif _CSI_PREFIX.fullmatch(text, offset):
                if not final and len(text) - offset <= _MAX_PARAMETERS:
                    self._partial = text[end:]
                return

    def _set_colors(self, params: str) -> None:
        if len(params) > _MAX_PARAMETERS or not _SGR_PARAMETERS.fullmatch(params):
            return
        parameters = iter(params.split(";"))
        for parameter in parameters:
            if ":" in parameter:
                target, mode, *values = parameter.split(":")
                if mode == "2" and len(values) == 4 and values[0] in ("", "0"):
                    values = values[1:]
                self._set_extended(target, mode, values)
                continue

            code = int(parameter or "0")
            if code == 0:
                self._foreground = self._background = None
            elif 30 <= code <= 37:
                self._foreground = _PALETTE[code - 30]
            elif 40 <= code <= 47:
                self._background = _PALETTE[code - 40]
            elif 90 <= code <= 97:
                self._foreground = _PALETTE[code - 90 + 8]
            elif 100 <= code <= 107:
                self._background = _PALETTE[code - 100 + 8]
            elif code == 39:
                self._foreground = None
            elif code == 49:
                self._background = None
            elif code in (38, 48, 58):
                mode = next(parameters, "")
                count = {"2": 3, "5": 1}.get(mode, 0)
                self._set_extended(str(code), mode, list(islice(parameters, count)))

    def _set_extended(self, target: str, mode: str, values: Sequence[str]) -> None:
        if target in ("38", "48") and (color := _extended_color(mode, values)) is not None:
            if target == "38":
                self._foreground = color
            else:
                self._background = color
