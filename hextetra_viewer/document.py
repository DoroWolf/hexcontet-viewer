"""数据源：文件（mmap 只读、零拷贝翻页）或标准输入，并支持等长原地编辑。

查看时文件走只读 mmap（超大文件也不吃内存）；一旦调用 :meth:`Document.overwrite`
就把后端升级成可写的 ``bytearray``（copy-on-write，只做一次），同时记录撤销栈。
改写严格保持数据长度不变，所以偏移语义、槽位窗口与标准 Base64 对齐规则都不受影响。

写回源文件用「同目录临时文件 + :func:`os.replace`」原子替换，
``backup=True`` 时先把原文件复制成 ``<文件名>.bak``。
"""

from __future__ import annotations

import mmap
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from .core import changed_cell_indices, format_count, format_offset

EMPTY_DATA = b""

BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True, slots=True)
class Edit:
    offset: int
    before: bytes
    after: bytes

    @property
    def length(self) -> int:
        return len(self.after)

    @property
    def end(self) -> int:
        return self.offset + self.length

    def byte_changes(self) -> Iterator[tuple[int, int, int]]:
        for index, (old, new) in enumerate(zip(self.before, self.after)):
            if old != new:
                yield self.offset + index, old, new


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - 权限/占用等极端情况
        pass


@dataclass(slots=True)
class Document:
    path: Path | None
    size: int
    _data: bytes | bytearray | mmap.mmap
    _handle: BinaryIO | None = None
    _edits: list[Edit] = field(default_factory=list)
    _undone: list[Edit] = field(default_factory=list)

    @classmethod
    def open(cls, path: Path) -> Document:
        handle = path.open("rb")
        try:
            size = path.stat().st_size
            data: bytes | mmap.mmap
            if size > 0:
                data = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            else:
                data = EMPTY_DATA
        except OSError:
            handle.close()
            raise
        return cls(path=path, size=size, _data=data, _handle=handle)

    @classmethod
    def from_stdin(cls) -> Document:
        data = sys.stdin.buffer.read()
        return cls(path=None, size=len(data), _data=data, _handle=None)

    @property
    def data(self) -> bytes | bytearray | mmap.mmap:
        return self._data

    @property
    def display_name(self) -> str:
        return str(self.path) if self.path is not None else "standard input"

    @property
    def writable(self) -> bool:
        return self.path is not None

    @property
    def dirty(self) -> bool:
        return bool(self._edits)

    @property
    def edits(self) -> tuple[Edit, ...]:
        return tuple(self._edits)

    @property
    def changed_offsets(self) -> frozenset[int]:
        return frozenset(
            offset for edit in self._edits for offset, _old, _new in edit.byte_changes()
        )

    @property
    def changed_cells(self) -> frozenset[int]:
        cells: set[int] = set()
        for edit in self._edits:
            cells |= changed_cell_indices(edit.offset, edit.before, edit.after)
        return frozenset(cells)

    def view(self, offset: int = 0, length: int | None = None) -> memoryview:
        view = memoryview(self._data).toreadonly()
        end = len(view) if length is None else offset + length
        return view[offset:end]


    def overwrite(self, offset: int, payload: bytes) -> Edit:
        patch = bytes(payload)
        if not patch:
            raise ValueError("bytys to write cannot be empty")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if offset + len(patch) > self.size:
            raise ValueError(
                f"offset {format_offset(offset, 1)} + {format_count(len(patch))} bytes"
                f" exceeds {self.display_name} ({format_count(self.size)} bytes)"
            )

        data = self._make_mutable()
        before = bytes(data[offset : offset + len(patch)])
        data[offset : offset + len(patch)] = patch
        edit = Edit(offset=offset, before=before, after=patch)
        self._edits.append(edit)
        self._undone.clear()
        return edit

    def undo(self) -> Edit | None:
        if not self._edits:
            return None
        edit = self._edits.pop()
        self._restore(edit.before, edit.offset)
        self._undone.append(edit)
        return edit

    def redo(self) -> Edit | None:
        if not self._undone:
            return None
        edit = self._undone.pop()
        self._restore(edit.after, edit.offset)
        self._edits.append(edit)
        return edit

    def _restore(self, payload: bytes, offset: int) -> None:
        data = self._make_mutable()
        data[offset : offset + len(payload)] = payload

    def _make_mutable(self) -> bytearray:
        data = self._data
        if isinstance(data, bytearray):
            return data
        buffer = bytearray(data)
        if isinstance(data, mmap.mmap) and not data.closed:
            data.close()
        self._data = buffer
        return buffer

    def _release(self) -> None:
        data = self._data
        if isinstance(data, mmap.mmap) and not data.closed:
            data.close()
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None

    def _reopen(self) -> None:
        if self.path is None:
            return
        self._release()
        try:
            fresh = Document.open(self.path)
        except OSError:  # pragma: no cover - 文件被删除/权限变化时保持内存后端
            return
        self._data = fresh.data
        self._handle = fresh._handle

    def save(self, *, backup: bool = False) -> bool:
        if self.path is None:
            raise OSError("standard input cannot be saved")
        if not self._edits:
            return False

        path = self.path
        payload = bytes(self._make_mutable())
        handle, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if backup:
                shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
        except BaseException:
            _remove_quietly(temp_name)
            raise

        # Windows 上源文件仍被打开时 os.replace 会失败，先释放句柄再替换
        self._release()
        try:
            os.replace(temp_name, path)
        except BaseException:
            _remove_quietly(temp_name)
            self._reopen()
            raise

        self._edits.clear()
        self._undone.clear()
        self._reopen()
        return True

    def close(self) -> None:
        self._release()

    def __enter__(self) -> Document:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False
