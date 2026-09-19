from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass

from .core import BITS_PER_CELL, Cell, read_cell, read_line
from .document import BACKUP_SUFFIX, Document
from .render import (
    DumpView,
    Palette,
    display_digits,
    display_number,
    display_offset,
    display_octal,
)

ALT_SCREEN_ON = "\x1b[?1049h"
ALT_SCREEN_OFF = "\x1b[?1049l"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
HOME = "\x1b[H"
CLEAR_TO_EOL = "\x1b[K"
CLEAR_TO_END = "\x1b[J"

OCTAL_DIGITS = "01234567"

WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "I": "pgup",
    "Q": "pgdn",
    "S": "delete",
}

POSIX_KEYS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "1~": "home",
    "4~": "end",
    "5~": "pgup",
    "6~": "pgdn",
    "3~": "delete",
}

HINTS = "←→ cell  ↑↓ PgUp/PgDn  Home/End  Enter edit  u undo  ^R redo  s save  q quit"


def last_data_cell(size: int) -> int:
    return max(0, -(-size * 8 // BITS_PER_CELL) - 1)


def write_cell(
    document: Document,
    index: int,
    value: int,
    *,
    human_compatible: bool = False,
) -> None:
    if not 0 <= value <= 0x3F:
        raise ValueError(f"value must be within 0-63: {value}")
    bit_start = index * BITS_PER_CELL
    if bit_start >= document.size * 8:
        raise ValueError("cell lies beyond the end of the data")

    first_byte = bit_start // 8
    last_byte = min((bit_start + BITS_PER_CELL - 1) // 8, document.size - 1)
    chunk = bytes(document.data[first_byte : last_byte + 1])
    shift_in_chunk = bit_start - first_byte * 8
    inside = min(BITS_PER_CELL, len(chunk) * 8 - shift_in_chunk)
    padding = BITS_PER_CELL - inside
    if padding and value & ((1 << padding) - 1):
        step = display_number(1 << padding, human_compatible=human_compatible)
        raise ValueError(f"a tail cell padded with zeros only accepts multiples of {step}")

    effective = value >> padding
    shift = max(len(chunk) * 8 - shift_in_chunk - BITS_PER_CELL, 0)
    mask = ((1 << inside) - 1) << shift
    acc = int.from_bytes(chunk, "big")
    acc = (acc & ~mask) | (effective << shift)
    document.overwrite(first_byte, acc.to_bytes(len(chunk), "big"))


@dataclass
class Editor:

    document: Document
    view: DumpView
    backup: bool = False
    cursor: int = 0
    scroll: int = 0
    rows: int = 20
    mode: str = "browse"
    pending: str = ""
    buffer: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        self.cursor = min(self._line_first_cell(0), last_data_cell(self.document.size))
        self._follow_cursor()

    def _cell_byte(self, index: int) -> int:
        return index * BITS_PER_CELL // 8

    def _cursor_line(self) -> int:
        return max(0, (self._cell_byte(self.cursor) - self.view.offset) // self.view.width)

    def _line_first_cell(self, line: int) -> int:
        start = self.view.offset + line * self.view.width
        return -(-start * 8 // BITS_PER_CELL)

    def _last_line(self) -> int:
        remaining = max(0, self.document.size - self.view.offset)
        return max(0, -(-remaining // self.view.width) - 1)

    def _follow_cursor(self) -> None:
        line = self._cursor_line()
        if line < self.scroll:
            self.scroll = line
        elif line >= self.scroll + self.rows:
            self.scroll = line - self.rows + 1
        limit = max(0, self._last_line() - self.rows + 1)
        self.scroll = max(0, min(self.scroll, limit))

    def resize(self, rows: int) -> None:
        self.rows = max(1, rows)
        self._follow_cursor()

    def move_cells(self, step: int) -> None:
        limit = last_data_cell(self.document.size)
        self.cursor = max(0, min(self.cursor + step, limit))
        self._follow_cursor()

    def go_to_line(self, line: int) -> None:
        line = max(0, min(line, self._last_line()))
        self.cursor = min(self._line_first_cell(line), last_data_cell(self.document.size))
        self._follow_cursor()

    def active_view(self) -> DumpView:
        return self.view.with_cursor(self.cursor).with_changes(
            self.document.changed_offsets,
            self.document.changed_cells,
        )

    def frame(self, rows: int) -> list[str]:
        self.resize(rows)
        active = self.active_view()
        lines = list(active.header_lines())
        last = self._last_line()
        for index in range(self.scroll, self.scroll + self.rows):
            if index > last:  # 数据已经结束：留空白行，不刷无意义的偏移
                lines.append("")
                continue
            offset = active.offset + index * active.width
            lines.append(active.line(read_line(self.document.data, offset, active.width)))
        lines.append(self.status_text())
        return lines

    def status_text(self) -> str:
        if self.mode == "confirm":
            return f"[{self.pending}] {self.message} [y/N]"
        if self.mode == "edit":
            # 输入永远是真实数字 0-7，这里只按显示字母表把 7 写成 8
            typed = display_digits(
                self.buffer, human_compatible=self.view.human_compatible
            ) or "_"
            return (
                f" octal {typed}   (digits 0-{"7" if self.view.human_compatible else "8"}, Enter to apply, Esc to cancel)"
            )
        marker = "*" if self.document.dirty else " "
        offset = self._cell_byte(self.cursor)
        position = f"{self._shown_offset(offset)}  cell {self._shown_number(self.cursor)}"
        message = f"  {self.message}" if self.message else ""
        return f"{marker}{position}{message}  |  {HINTS}"

    def _shown(self, cell: Cell) -> str:
        return display_octal(cell.octal, human_compatible=self.view.human_compatible)

    def _shown_number(self, value: int) -> str:
        return display_number(value, human_compatible=self.view.human_compatible)

    def _shown_offset(self, offset: int) -> str:
        return display_offset(
            offset, self.view.offset_width, human_compatible=self.view.human_compatible
        )

    def handle_key(self, key: str) -> str:
        if self.mode == "confirm":
            return self._handle_confirm(key)
        if self.mode == "edit":
            self._handle_edit(key)
            return "continue"
        return self._handle_browse(key)

    def _handle_browse(self, key: str) -> str:
        if key in ("q", "ctrl-c"):
            return self._request_quit()
        if key in ("enter", "i"):
            self._start_edit()
        elif key == "left":
            self.move_cells(-1)
        elif key == "right":
            self.move_cells(1)
        elif key == "up":
            self.go_to_line(self._cursor_line() - 1)
        elif key == "down":
            self.go_to_line(self._cursor_line() + 1)
        elif key == "pgup":
            self.go_to_line(self._cursor_line() - max(1, self.rows - 1))
        elif key == "pgdn":
            self.go_to_line(self._cursor_line() + max(1, self.rows - 1))
        elif key == "home":
            self.go_to_line(0)
        elif key == "end":
            self.go_to_line(self._last_line())
        elif key == "u":
            self._undo()
        elif key == "ctrl-r":
            self._redo()
        elif key == "s":
            self._request_save()
        return "continue"

    def _start_edit(self) -> None:
        cell = read_cell(self.document.data, self.cursor)
        if cell.is_padding:
            self.message = (
                f"cell {self._shown_number(self.cursor)} is a '=' padding slot, nothing to edit"
            )
            return
        self.mode = "edit"
        self.buffer = ""

    def _handle_edit(self, key: str) -> None:
        if key == "esc":
            self.mode = "browse"
            self.buffer = ""
            self.message = "edit cancelled"
        elif key == "backspace":
            self.buffer = self.buffer[:-1]
        elif key == "enter":
            if self.buffer:
                self._apply_octal()
        elif len(key) == 1 and key in OCTAL_DIGITS:
            self.buffer = (self.buffer + key)[-2:]

    def _apply_octal(self) -> None:
        digits = self.buffer
        self.buffer = ""
        self._apply_value(int(digits, 8) if digits else 0)

    def _apply_value(self, value: int) -> None:
        index = self.cursor
        before = read_cell(self.document.data, index)
        try:
            write_cell(
                self.document,
                index,
                value,
                human_compatible=self.view.human_compatible,
            )
        except ValueError as error:
            self.mode = "browse"
            self.message = f"cannot edit cell {self._shown_number(index)}: {error}"
            return
        after = read_cell(self.document.data, index)
        self.mode = "browse"
        self.message = (
            f"cell {self._shown_number(index)}: {self._shown(before)} {before.char}"
            f" -> {self._shown(after)} {after.char}"
        )
        self.move_cells(1)

    def _undo(self) -> None:
        edit = self.document.undo()
        if edit is None:
            self.message = "nothing to undo"
            return
        self.message = (
            f"undone: restored {self._shown_number(edit.length)} byte(s)"
            f" at {self._shown_offset(edit.offset)}"
        )

    def _redo(self) -> None:
        edit = self.document.redo()
        if edit is None:
            self.message = "nothing to redo"
            return
        self.message = (
            f"redone: rewrote {self._shown_number(edit.length)} byte(s)"
            f" at {self._shown_offset(edit.offset)}"
        )

    def _request_save(self) -> None:
        if not self.document.dirty:
            self.message = "no changes to save"
            return
        extra = f" (backup: <name>{BACKUP_SUFFIX})" if self.backup else ""
        self.pending = "save"
        self.mode = "confirm"
        edits = self._shown_number(len(self.document.edits))
        self.message = (
            f"write {edits} edit(s) to {self.document.display_name}{extra}?"
        )

    def _request_quit(self) -> str:
        if not self.document.dirty:
            return "quit"
        self.pending = "quit"
        self.mode = "confirm"
        self.message = (
            f"discard {self._shown_number(len(self.document.edits))} unsaved edit(s)?"
        )
        return "continue"

    def _handle_confirm(self, key: str) -> str:
        action = self.pending
        self.pending = ""
        self.mode = "browse"
        if key not in ("y", "Y", "enter"):
            self.message = "cancelled"
            return "continue"
        if action == "quit":
            return "quit"
        self._save()
        return "continue"

    def _save(self) -> None:
        try:
            written = self.document.save(backup=self.backup)
        except OSError as error:
            self.message = f"save failed: {error}"
            return
        if not written:
            self.message = "no changes to save"
            return
        suffix = ""
        if self.backup and self.document.path is not None:
            suffix = f" (backup: {self.document.path.name}{BACKUP_SUFFIX})"
        self.message = f"saved {self.document.display_name}{suffix}"


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # pragma: no cover - 老系统/被重定向时退化成普通输出
        pass


class Terminal:
    def __init__(self) -> None:
        self._stream = sys.stdout
        self._termios = None
        self._saved = None

    def __enter__(self) -> Terminal:
        _enable_windows_ansi()
        if os.name != "nt":
            import termios
            import tty

            self._termios = termios
            fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        self.write(ALT_SCREEN_ON + HIDE_CURSOR)
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        if self._saved is not None and self._termios is not None:
            self._termios.tcsetattr(sys.stdin.fileno(), self._termios.TCSADRAIN, self._saved)
        return False

    def write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()

    def read_key(self, timeout: float | None = None) -> str | None:
        if os.name == "nt":
            return self._read_key_windows(timeout)
        return self._read_key_posix(timeout)

    @staticmethod
    def _normalize(char: str) -> str | None:
        if char == "\x03":
            return "ctrl-c"
        if char == "\x12":
            return "ctrl-r"
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x1b":
            return "esc"
        if char in ("\x08", "\x7f"):
            return "backspace"
        if char == "\t":
            return "tab"
        return char if char.isprintable() else None

    def _read_key_windows(self, timeout: float | None) -> str | None:
        import msvcrt

        deadline = None if timeout is None else time.monotonic() + timeout
        while not msvcrt.kbhit():
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.02)
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            return WINDOWS_KEYS.get(msvcrt.getwch())
        return self._normalize(char)

    def _read_key_posix(self, timeout: float | None) -> str | None:
        import select

        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], timeout)[0]:
            return None
        char = os.read(fd, 1).decode("utf-8", "ignore")
        if char != "\x1b":
            return self._normalize(char)
        return self._read_escape_sequence(fd)

    @staticmethod
    def _read_escape_sequence(fd: int) -> str:
        import select

        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        sequence = os.read(fd, 1).decode("utf-8", "ignore")
        if sequence != "[":
            return "esc"
        while len(sequence) < 4:
            if not select.select([fd], [], [], 0.05)[0]:
                return "esc"
            tail = os.read(fd, 1).decode("utf-8", "ignore")
            sequence += tail
            if tail.isalpha() or tail == "~":
                break
        return POSIX_KEYS.get(sequence[1:], None)


def run_tui(
    document: Document,
    *,
    width: int = 12,
    offset: int = 0,
    backup: bool = False,
    show_ascii: bool = True,
    human_compatible: bool = False,
) -> int:
    if document.size == 0:
        print("error: nothing to edit in an empty file", file=sys.stderr)
        return 1

    view = DumpView.create(
        document.size,
        width=width,
        offset=offset,
        palette=Palette(True),
        show_ascii=show_ascii,
        human_compatible=human_compatible,
    )
    editor = Editor(document=document, view=view, backup=backup)

    try:
        with Terminal() as terminal:
            while True:
                size = shutil.get_terminal_size()
                rows = max(1, size.lines - len(view.header_lines()) - 1)
                lines = editor.frame(rows)
                terminal.write(
                    HOME
                    + "".join(text + CLEAR_TO_EOL + "\r\n" for text in lines[:-1])
                    + lines[-1]
                    + CLEAR_TO_EOL
                    + CLEAR_TO_END
                )
                key = terminal.read_key()
                if key is not None and editor.handle_key(key) == "quit":
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if document.dirty:
            discards = display_number(
                len(document.edits), human_compatible=human_compatible
            )
            print(f"note: {discards} unsaved edit(s) discarded", file=sys.stderr)
    return 0


