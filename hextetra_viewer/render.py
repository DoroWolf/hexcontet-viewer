from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Iterator

from .core import (
    CELLS_PER_GROUP,
    DUODECIMAL_DIGITS,
    OCTAL_WIDTH,
    OFFSET_ALPHABET,
    Buffer,
    Cell,
    Line,
    Slot,
    format_count,
    format_offset,
    read_line,
    slot_window,
)

#: ANSI SGR 常量
RESET = "\x1b[0m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
REVERSE = "\x1b[7m"

#: 列与单元之间的分隔符
OFFSET_SEP = "  "
CELL_SEP = " "
GROUP_SEP = "  "
COLUMN_SEP = "  |  "

#: 占位符与空白槽位的写法
PLACEHOLDER_OCTAL = ".."
PLACEHOLDER_CHAR = "."
BLANK_OCTAL = " " * OCTAL_WIDTH
BLANK_CHAR = " "
#: ``human_compatible`` 时的标准十二进制数字，同时也是 ``0d`` 偏移输入用的字母表
HUMAN_OFFSET_ALPHABET = DUODECIMAL_DIGITS
#: 偏移列最少显示多少位十二进制数字
MIN_OFFSET_DIGITS = 8



@dataclass(frozen=True, slots=True)
class Palette:
    enabled: bool = False

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled else text

    def offset(self, text: str) -> str:
        return self._wrap(DIM, text)

    def base64(self, text: str) -> str:
        return self._wrap(CYAN, text)

    def muted(self, text: str) -> str:
        return self._wrap(DIM, text)

    def ascii(self, text: str, *, printable: bool) -> str:
        return text if printable else self._wrap(DIM, text)

    def highlight(self, text: str) -> str:
        return self._wrap(REVERSE, text)


def resolve_palette(mode: str = "auto") -> Palette:
    if mode == "always":
        return Palette(True)
    if mode == "never":
        return Palette(False)
    if os.environ.get("NO_COLOR"):
        return Palette(False)
    try:
        return Palette(bool(sys.stdout.isatty()))
    except (AttributeError, ValueError):  # pragma: no cover - 输出被替换时的兜底
        return Palette(False)


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def display_digits(text: str, *, human_compatible: bool = False) -> str:
    if human_compatible:
        return text
    return text.replace("7", "8")


def display_octal(octal: str, *, human_compatible: bool = False) -> str:
    if len(octal) != OCTAL_WIDTH:
        return octal
    return display_digits(octal, human_compatible=human_compatible)


def offset_digits(size: int) -> int:
    digits = MIN_OFFSET_DIGITS
    limit = 12 ** digits
    while size > limit:
        digits += 1
        limit *= 12
    return digits


def display_number(value: int, *, human_compatible: bool = False) -> str:
    if human_compatible:
        return str(value)
    return format_count(value)


def display_offset(
    offset: int,
    digits: int,
    *,
    prefix: bool = True,
    human_compatible: bool = False,
) -> str:
    alphabet = HUMAN_OFFSET_ALPHABET if human_compatible else OFFSET_ALPHABET
    return format_offset(offset, digits, prefix, alphabet)


def pad_to(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def fit_title(long_title: str, short_title: str, width: int) -> str:
    return long_title if display_width(long_title) <= width else short_title


@dataclass(frozen=True, slots=True)
class DumpView:
    width: int
    offset: int
    slots: int
    offset_width: int
    show_ascii: bool
    palette: Palette
    human_compatible: bool = False
    changed: frozenset[int] = frozenset()
    changed_cells: frozenset[int] = frozenset()
    cursor: int | None = None

    @classmethod
    def create(
        cls,
        size: int,
        *,
        width: int,
        offset: int,
        palette: Palette,
        show_ascii: bool = True,
        human_compatible: bool = False,
        changed: frozenset[int] | None = None,
        changed_cells: frozenset[int] | None = None,
        cursor: int | None = None,
    ) -> DumpView:
        return cls(
            width=width,
            offset=offset,
            slots=slot_window(width, offset),
            offset_width=offset_digits(size),
            show_ascii=show_ascii,
            palette=palette,
            human_compatible=human_compatible,
            changed=frozenset(changed) if changed else frozenset(),
            changed_cells=frozenset(changed_cells) if changed_cells else frozenset(),
            cursor=cursor,
        )

    def with_cursor(self, cursor: int | None) -> DumpView:
        return replace(self, cursor=cursor)

    def with_changes(
        self,
        changed: Iterable[int] = (),
        changed_cells: Iterable[int] = (),
    ) -> DumpView:
        return replace(
            self,
            changed=frozenset(changed),
            changed_cells=frozenset(changed_cells),
        )

    @property
    def offset_field_width(self) -> int:
        return display_width(self._offset_text(0))

    @property
    def groups(self) -> int:
        return self.slots // CELLS_PER_GROUP

    @property
    def middle_width(self) -> int:
        per_group = CELLS_PER_GROUP * OCTAL_WIDTH + (CELLS_PER_GROUP - 1) * len(CELL_SEP)
        return self.groups * per_group + (self.groups - 1) * len(GROUP_SEP)

    @property
    def base64_width(self) -> int:
        return self.groups * CELLS_PER_GROUP + (self.groups - 1) * len(GROUP_SEP)

    def header_lines(self) -> list[str]:
        titles = (
            fit_title("Offset", "OFF", self.offset_field_width),
            fit_title("Octal", "OCT", self.middle_width),
            fit_title("Base64", "B64", self.base64_width),
            fit_title("ASCII", "ASC", self.width),
        )
        row = (
            pad_to(titles[0], self.offset_field_width)
            + OFFSET_SEP
            + pad_to(titles[1], self.middle_width)
            + COLUMN_SEP
            + pad_to(titles[2], self.base64_width)
        )
        if self.show_ascii:
            row += COLUMN_SEP + titles[3]
        return [self.palette.muted(row)]

    def line(self, item: Line) -> str:
        groups = range(0, self.slots, CELLS_PER_GROUP)
        middle = GROUP_SEP.join(
            CELL_SEP.join(self._octal_text(slot) for slot in item.slots[start : start + CELLS_PER_GROUP])
            for start in groups
        )
        encoded = GROUP_SEP.join(
            "".join(self._char_text(slot) for slot in item.slots[start : start + CELLS_PER_GROUP])
            for start in groups
        )
        text = (
            self.palette.offset(self._offset_text(item.offset))
            + OFFSET_SEP
            + middle
            + COLUMN_SEP
            + encoded
        )
        if self.show_ascii:
            return text + COLUMN_SEP + self._ascii_text(item.ascii_text, item.offset)
        return text.rstrip()

    def _offset_text(self, offset: int) -> str:
        return display_offset(
            offset,
            self.offset_width,
            prefix=False,
            human_compatible=self.human_compatible,
        )

    def render(
        self,
        data: Buffer,
        *,
        header: bool = True,
        max_bytes: int | None = None,
        max_lines: int | None = None,
    ) -> Iterator[str]:
        size = len(data)
        if header:
            yield from self.header_lines()

        available = max(0, size - self.offset)
        shown = available if max_bytes is None else min(available, max(0, max_bytes))
        if max_lines is not None:
            shown = min(shown, max(0, max_lines) * self.width)
        if shown <= 0:
            return

        for index in range(-(-shown // self.width)):
            offset = self.offset + index * self.width
            consumed = index * self.width
            remaining = None if max_bytes is None else max_bytes - consumed
            yield self.line(read_line(data, offset, self.width, max_bytes=remaining))

    def _render_octal(self, text: str) -> str:
        return display_octal(text, human_compatible=self.human_compatible)

    def _mark(self, text: str, *, index: int) -> str:
        """命中光标优先反显，其次是真正被改写过的单元"""
        if self.cursor is not None and index == self.cursor:
            return self.palette.highlight(text)
        if index in self.changed_cells:
            return self.palette.highlight(text)
        return text

    def _octal_text(self, slot: Slot) -> str:
        if slot is None:
            return BLANK_OCTAL
        if isinstance(slot, Cell):
            if slot.is_padding:
                return self.palette.muted(slot.octal)
            return self._mark(self._render_octal(slot.octal), index=slot.index)
        return self.palette.muted(PLACEHOLDER_OCTAL)

    def _char_text(self, slot: Slot) -> str:
        if slot is None:
            return BLANK_CHAR
        if isinstance(slot, Cell):
            if slot.is_padding:
                return self.palette.muted(slot.char)
            return self._mark(self.palette.base64(slot.char), index=slot.index)
        return self.palette.muted(PLACEHOLDER_CHAR)

    def _ascii_text(self, text: str, offset: int) -> str:
        if not self.changed:  # 常规查看的快路径：不逐字符判断改动
            return "".join(self.palette.ascii(char, printable=char != ".") for char in text)
        parts = []
        for index, char in enumerate(text):
            colored = self.palette.ascii(char, printable=char != ".")
            if offset + index in self.changed:
                colored = self.palette.highlight(colored)
            parts.append(colored)
        return "".join(parts)
