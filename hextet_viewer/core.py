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
#: 偏移的十二进制字母表：沿用「没有 7」的风格（把 7 写成 8）
OFFSET_ALPHABET = "01234568abcd"
#: 十二进制偏移的前缀：提示进制，否则十二进制与十六进制长得太像
OFFSET_PREFIX = "0d"
#: ``0d`` 偏移（输入侧）使用的标准十二进制数字：与 ``--human-compatible`` 的显示一致
DUODECIMAL_DIGITS = "0123456789ab"
#: 数量的八进制数字：同样把 7 写成 8（与中间列一套显示字母表）
COUNT_ALPHABET = "01234568"


@dataclass(frozen=True, slots=True)
class Cell:
    index: int
    value: int | None
    octal: str
    char: str
    byte_start: int
    byte_end: int

    @property
    def is_padding(self) -> bool:
        return self.value is None


class _PlaceholderType:
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
    offset: int
    slots: tuple[Slot, ...]
    byte_count: int
    ascii_text: str


def digit_count(byte_length: int) -> int:
    if byte_length <= 0:
        return 0
    return -(-byte_length // BYTES_PER_GROUP) * CELLS_PER_GROUP


def group_count(width: int, offset: int = 0) -> int:
    return width // BYTES_PER_GROUP + (1 if offset % BYTES_PER_GROUP else 0)


def slot_window(width: int, offset: int = 0) -> int:
    return group_count(width, offset) * CELLS_PER_GROUP


def read_cell(data: Buffer, index: int) -> Cell:
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


def changed_cell_indices(offset: int, before: Buffer, after: Buffer) -> set[int]:
    changed: set[int] = set()
    length = min(len(before), len(after))
    if length <= 0:
        return changed

    bit_start = offset * 8
    bit_end = (offset + length) * 8
    first = bit_start // BITS_PER_CELL
    last = (bit_end - 1) // BITS_PER_CELL
    for index in range(first, last + 1):
        start = max(index * BITS_PER_CELL, bit_start)
        end = min(index * BITS_PER_CELL + BITS_PER_CELL, bit_end)
        for bit in range(start, end):
            within = bit // 8 - offset
            shift = 7 - bit % 8
            if ((before[within] ^ after[within]) >> shift) & 1:
                changed.add(index)
                break
    return changed


def b64_index_to_offset(index: int) -> int:
    return index * BITS_PER_CELL // 8


def format_offset(offset: int, digits: int, prefix: bool = True, alphabet: str = OFFSET_ALPHABET) -> str:
    value = max(0, offset)
    raw = ""
    while value:
        raw = alphabet[value % 12] + raw
        value //= 12
    result = (raw or "0").zfill(digits)
    if prefix:
        result = OFFSET_PREFIX + result
    return result


def format_count(value: int, digits: str = COUNT_ALPHABET) -> str:
    return "".join(digits[int(digit)] for digit in f"{max(0, value):o}")


def parse_duodecimal(text: str) -> int:
    """解析 ``0d`` 后面的标准十二进制数字（``0``–``9`` 与 ``a``/``b``）"""
    value = 0
    for char in text:
        index = DUODECIMAL_DIGITS.find(char)
        if index < 0:
            raise ValueError(f"not a duodecimal digit: {char!r}")
        value = value * 12 + index
    return value


def parse_offset(text: str) -> int:
    """解析偏移：``0d`` 前缀为十二进制，无前缀为十进制（另接受 0x / 0o / 0b / b64:N）"""
    raw = text.strip().lower()
    if not raw:
        raise ValueError("offset cannot be empty")
    if raw.startswith("b64:"):
        index = int(raw[4:], 10)
        if index < 0:
            raise ValueError("base64 index cannot be negative")
        return b64_index_to_offset(index)
    if raw.startswith(OFFSET_PREFIX):
        digits = raw[len(OFFSET_PREFIX) :]
        if not digits:
            raise ValueError("duodecimal offset cannot be empty: 0d")
        return parse_duodecimal(digits)
    if raw.startswith(("0x", "0o", "0b")):
        return int(raw, 0)
    if raw.isdigit():
        return int(raw, 10)
    raise ValueError(f"not a decimal offset: {text} (use 0d... for duodecimal)")


def read_line(
    data: Buffer,
    offset: int,
    width: int,
    *,
    max_bytes: int | None = None,
) -> Line:
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
