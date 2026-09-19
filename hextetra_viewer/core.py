"""64 进制查看器的核心算法：在字节流与 6 bit 显示单元之间换算。

一个字节是 8 bit，一个 64 进制数字是 6 bit，两者不对齐；
3 个字节 = 24 bit = 4 个 64 进制数字，这是本项目的最小对齐单位。

64 进制数字的取值范围是 0-63，正好可以写成两位八进制（00-77），
所以中间列用两位八进制书写，右栏同时给出对应的标准 Base64（RFC 4648）字符。

单元（cell）网格始终锚定在数据第 0 字节上，因此右栏 Base64 严格等于
标准 Base64 编码在同一位置上的片段，不会因为起始偏移不同而整体错位；
偏移没有按 3 字节对齐时，行首那些“起始字节落在上一行”的单元渲染成占位符。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 标准 Base64 字母表（RFC 4648）
BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

#: 一个 64 进制数字的位数
BITS_PER_CELL = 6
#: 一个对齐分组包含的字节数
BYTES_PER_GROUP = 3
#: 一个对齐分组包含的 64 进制数字个数
CELLS_PER_GROUP = 4
#: 两位八进制的字符宽度（63 写成 "77"）
OCTAL_WIDTH = 2
#: 末组 '=' 补位在中间列里的写法
PADDING_OCTAL = "--"
#: 末组 '=' 补位在右栏里的写法
PADDING_CHAR = "="


@dataclass(frozen=True, slots=True)
class Cell:
    """一个 6 bit 显示单元，也就是一个 64 进制数字。

    ``value`` 为 ``None`` 表示该槽位是末组的 '=' 补位，没有对应的 6 bit。
    ``byte_start`` / ``byte_end`` 是该单元覆盖的字节区间（右开，已按数据长度截断）。
    """

    index: int
    value: int | None
    octal: str
    char: str
    byte_start: int
    byte_end: int

    @property
    def is_padding(self) -> bool:
        """是否是末组的 '=' 补位槽位"""
        return self.value is None


class _PlaceholderType:
    """占位符哨兵：该槽位属于上一行的单元，本行只占位不重复显示"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试时可见
        return "PLACEHOLDER"


#: 行首未对齐时使用的占位哨兵
PLACEHOLDER = _PlaceholderType()

#: 一行里的一个槽位：数据单元 / 占位符 / 空白（该行不涉及）
Slot = Cell | _PlaceholderType | None

#: 可读取的字节来源；mmap.mmap 也属于 bytes-like，运行时同样可直接传入
Buffer = bytes | bytearray | memoryview


@dataclass(frozen=True, slots=True)
class Line:
    """一行转储：从 ``offset`` 开始、长度不超过 ``width`` 的字节窗口。

    ``slots`` 已按窗口补齐（``None`` 表示空白槽位），长度恒等于
    :func:`slot_window` 的结果，逐行渲染时列宽才是稳定的。
    """

    offset: int
    slots: tuple[Slot, ...]
    byte_count: int
    ascii_text: str


def digit_count(byte_length: int) -> int:
    """``byte_length`` 个字节对应的 64 进制数字总数（含末组的 '=' 补位）"""
    if byte_length <= 0:
        return 0
    return -(-byte_length // BYTES_PER_GROUP) * CELLS_PER_GROUP


def group_count(width: int, offset: int = 0) -> int:
    """一行需要显示多少个 3 字节分组。

    偏移没有按 3 字节对齐时会比 ``width // 3`` 多一组（行首的不完整分组）。
    """
    return width // BYTES_PER_GROUP + (1 if offset % BYTES_PER_GROUP else 0)


def slot_window(width: int, offset: int = 0) -> int:
    """一行的槽位总数，渲染时用它来补齐空白，保证逐行列对齐"""
    return group_count(width, offset) * CELLS_PER_GROUP


def read_cell(data: Buffer, index: int) -> Cell:
    """读取第 ``index`` 个 64 进制数字（含 '=' 补位槽位）。

    单元按数据第 0 字节对齐：第 n 个单元覆盖 bit ``[n*6, n*6+6)``；
    末组不足 3 字节时缺的 bit 按 0 补齐（与 Base64 的补位规则一致）。
    """
    size = len(data)
    group_start = (index // CELLS_PER_GROUP) * BYTES_PER_GROUP
    within = index % CELLS_PER_GROUP
    bit_start = index * BITS_PER_CELL
    byte_start = min(bit_start // 8, size)
    byte_end = min((bit_start + BITS_PER_CELL - 1) // 8 + 1, size)

    tail_bytes = min(BYTES_PER_GROUP, size - group_start)
    # 末组里真正有数据的数字个数：n 个字节对应 ceil(n*8/6) 个数字
    data_cells = (tail_bytes * 8 + BITS_PER_CELL - 1) // BITS_PER_CELL if tail_bytes > 0 else 0
    if within >= data_cells:
        return Cell(
            index=index,
            value=None,
            octal=PADDING_OCTAL,
            char=PADDING_CHAR,
            byte_start=byte_start,
            byte_end=byte_start,
        )

    chunk = data[group_start : group_start + BYTES_PER_GROUP]
    # 末组不足 3 字节时左移补 0，等价于按 3 字节分组做 Base64
    acc = int.from_bytes(chunk, "big") << ((BYTES_PER_GROUP - len(chunk)) * 8)
    value = (acc >> (BITS_PER_CELL * (CELLS_PER_GROUP - 1 - within))) & 0x3F
    return Cell(
        index=index,
        value=value,
        octal=f"{value:0{OCTAL_WIDTH}o}",
        char=BASE64_ALPHABET[value],
        byte_start=byte_start,
        byte_end=byte_end,
    )


def b64_index_to_offset(index: int) -> int:
    """Base64 字符下标 → 该字符覆盖的起始字节偏移"""
    return index * BITS_PER_CELL // 8


def parse_offset(text: str) -> int:
    """解析偏移文本：``0x1f`` / ``0o17`` / ``1f`` / ``31`` / ``b64:16``。

    ``b64:16`` 表示按 Base64 第 16 个字符所在的字节定位。
    无法解析时抛出 :class:`ValueError`。
    """
    raw = text.strip().lower()
    if not raw:
        raise ValueError("偏移不能为空")
    if raw.startswith("b64:"):
        index = int(raw[4:], 10)
        if index < 0:
            raise ValueError("Base64 下标不能为负数")
        return b64_index_to_offset(index)
    if raw.startswith(("0x", "0o", "0b")):
        return int(raw, 0)
    if raw.isdigit():
        return int(raw, 10)
    return int(raw, 16)


def read_line(
    data: Buffer,
    offset: int,
    width: int,
    *,
    max_bytes: int | None = None,
) -> Line:
    """读取 ``[offset, offset + width)`` 这一行。

    ``max_bytes`` 用于截断本行可见的字节数（``--length``），
    但不会改变槽位窗口，所以列宽仍然稳定。

    单元归属规则：一个单元由“包含它起始字节”的那一行显示。
    起始字节在 ``offset`` 之前的显示为 :data:`PLACEHOLDER`，
    在 ``offset + width`` 之后的留空。
    """
    size = len(data)
    window = slot_window(width, offset)
    if width <= 0 or offset >= size:
        return Line(offset=offset, slots=(None,) * window, byte_count=0, ascii_text="")

    end = min(offset + width, size)
    if max_bytes is not None:
        end = min(end, offset + max(0, max_bytes))
    if end <= offset:
        return Line(offset=offset, slots=(None,) * window, byte_count=0, ascii_text="")

    first_group = offset // BYTES_PER_GROUP
    last_group = (end - 1) // BYTES_PER_GROUP
    slots: list[Slot] = [None] * window
    for group in range(first_group, last_group + 1):
        base = (group - first_group) * CELLS_PER_GROUP
        for within in range(CELLS_PER_GROUP):
            cell = read_cell(data, group * CELLS_PER_GROUP + within)
            if cell.is_padding:
                # '=' 补位只出现在覆盖到数据末尾的那一组
                in_line = offset <= cell.byte_start < offset + width
                slots[base + within] = cell if in_line else None
            elif cell.byte_start < offset:
                slots[base + within] = PLACEHOLDER
            elif cell.byte_start < end:
                slots[base + within] = cell

    ascii_text = "".join(
        chr(byte) if 0x20 <= byte <= 0x7E else "." for byte in bytes(data[offset:end])
    )
    return Line(offset=offset, slots=tuple(slots), byte_count=end - offset, ascii_text=ascii_text)
