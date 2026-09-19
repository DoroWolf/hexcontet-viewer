"""把 :mod:`hextetra_viewer.core` 的行结构渲染成终端文本。

列布局（默认每行 12 字节）：

    偏移      中间列                                      | Base64              | ASCII
    00000000  20 24 11 03  20 24 11 03  ...              | QUJD QUJD ...       | ABC...

中间列每个单元固定 2 个字符、分组之间 2 个空格；右栏每个单元固定 1 个字符，
同样按 3 字节分组（4 个 Base64 字符）用 2 个空格分开，与中间列的分组一一对应。
两列宽度不同（2 字符 vs 1 字符），所以只保证分组对应、列首对齐。

着色只影响偏移列、Base64 列、占位符与不可打印字符，纯文本管道里可以完全关掉。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass
from typing import Iterator

from .core import (
    CELLS_PER_GROUP,
    OCTAL_WIDTH,
    Buffer,
    Cell,
    Line,
    Slot,
    read_line,
    slot_window,
)

#: ANSI SGR 常量
RESET = "\x1b[0m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"

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
OFFSET_ALPHABET = "01234568abcd"
HUMAN_OFFSET_ALPHABET = "0123456789ab"



@dataclass(frozen=True, slots=True)
class Palette:
    """终端配色；``enabled`` 为 False 时所有方法原样返回文本"""

    enabled: bool = False

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled else text

    def offset(self, text: str) -> str:
        """偏移列：暗色"""
        return self._wrap(DIM, text)

    def base64(self, text: str) -> str:
        """右栏 Base64 字符：青色"""
        return self._wrap(CYAN, text)

    def muted(self, text: str) -> str:
        """占位符与 '=' 补位：暗色"""
        return self._wrap(DIM, text)

    def ascii(self, text: str, *, printable: bool) -> str:
        """ASCII 列：不可打印字符用暗色"""
        return text if printable else self._wrap(DIM, text)


def resolve_palette(mode: str = "auto") -> Palette:
    """解析 ``--color`` 取值：auto 时看标准输出是否是终端，并尊重 NO_COLOR"""
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
    """终端显示宽度：全角字符占 2 列（用标准库 unicodedata，不引入依赖）"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad_to(text: str, width: int) -> str:
    """按显示宽度右补空格，中英混排的表头也能对齐"""
    return text + " " * max(0, width - display_width(text))


def fit_title(long_title: str, short_title: str, width: int) -> str:
    """列太窄时换成短标题，避免表头把列挤歪"""
    return long_title if display_width(long_title) <= width else short_title


@dataclass(frozen=True, slots=True)
class DumpView:
    """一次转储的布局参数，表头与数据行共用它来保证逐列对齐"""

    width: int
    offset: int
    slots: int
    offset_width: int
    show_ascii: bool
    palette: Palette
    human_compatible: bool = False

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
    ) -> DumpView:
        """按数据大小决定偏移列的宽度：< 4 GiB 用 8 位十六进制，否则 16 位"""
        return cls(
            width=width,
            offset=offset,
            slots=slot_window(width, offset),
            offset_width=8 if size <= 0xFFFFFFFF else 16,
            show_ascii=show_ascii,
            palette=palette,
            human_compatible=human_compatible,
        )

    @property
    def groups(self) -> int:
        """一行里的 3 字节分组个数"""
        return self.slots // CELLS_PER_GROUP

    @property
    def middle_width(self) -> int:
        """中间列的字符宽度"""
        per_group = CELLS_PER_GROUP * OCTAL_WIDTH + (CELLS_PER_GROUP - 1) * len(CELL_SEP)
        return self.groups * per_group + (self.groups - 1) * len(GROUP_SEP)

    @property
    def base64_width(self) -> int:
        """Base64 列的字符宽度"""
        return self.groups * CELLS_PER_GROUP + (self.groups - 1) * len(GROUP_SEP)

    def header_lines(self) -> list[str]:
        """表头：一行说明 + 一行列名"""
        titles = (
            fit_title("Offset", "OFF", self.offset_width),
            fit_title("Octal", "OCT", self.middle_width),
            fit_title("Base64", "B64", self.base64_width),
            fit_title("ASCII", "ASC", self.width),
        )
        row = (
            pad_to(titles[0], self.offset_width)
            + OFFSET_SEP
            + pad_to(titles[1], self.middle_width)
            + COLUMN_SEP
            + pad_to(titles[2], self.base64_width)
        )
        if self.show_ascii:
            row += COLUMN_SEP + titles[3]
        return [self.palette.muted(row)]

    def line(self, item: Line) -> str:
        """渲染一行"""
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
            return text + COLUMN_SEP + self._ascii_text(item.ascii_text)
        return text.rstrip()

    def _offset_text(self, offset: int) -> str:
            alphabet = HUMAN_OFFSET_ALPHABET if self.human_compatible else OFFSET_ALPHABET
            
            # 将 offset 转换为十二进制
            if offset == 0:
                digits = "0"
            else:
                raw_digits = []
                val = offset
                while val > 0:
                    raw_digits.append(alphabet[val % 12])
                    val //= 12
                digits = "".join(reversed(raw_digits))
            
            # 补齐指定宽度
            digits = digits.zfill(self.offset_width)
            
            # 如果 alphabet 刚好就是 HUMAN_OFFSET_ALPHABET，直接返回；
            # 若需要映射到其他字符集，可以在此保留 translate 逻辑。
            return digits

    def render(
        self,
        data: Buffer,
        *,
        header: bool = True,
        max_bytes: int | None = None,
        max_lines: int | None = None,
    ) -> Iterator[str]:
        """依次产出表头与数据行。

        ``max_bytes`` / ``max_lines`` 用于 ``--length`` / ``--lines``：
        只影响产出的行数，不影响槽位窗口，所以列宽始终一致。
        """
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
        """把八进制显示中的所有 ``7`` 替换成 ``8``，但保留真实 64 进制值不变。

        这只是显示层转换，不会改写 ``Cell.octal`` 之类的核心数据。
        """
        if self.human_compatible:
            return text
        if len(text) != OCTAL_WIDTH:
            return text
        return text.replace("7", "8")

    def _octal_text(self, slot: Slot) -> str:
        if slot is None:
            return BLANK_OCTAL
        if isinstance(slot, Cell):
            if slot.is_padding:
                return self.palette.muted(slot.octal)
            return self._render_octal(slot.octal)
        return self.palette.muted(PLACEHOLDER_OCTAL)

    def _char_text(self, slot: Slot) -> str:
        if slot is None:
            return BLANK_CHAR
        if isinstance(slot, Cell):
            return self.palette.muted(slot.char) if slot.is_padding else self.palette.base64(slot.char)
        return self.palette.muted(PLACEHOLDER_CHAR)

    def _ascii_text(self, text: str) -> str:
        return "".join(self.palette.ascii(char, printable=char != ".") for char in text)
