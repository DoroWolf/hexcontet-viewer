"""数据源：文件（mmap 只读、零拷贝翻页）或标准输入。

V1 只有只读能力，:meth:`Document.overwrite` / :meth:`Document.save`
是给将来的编辑功能预留的接缝：接上编辑时只要把只读的 mmap 换成
可写的 ``bytearray`` 后端，渲染层与定位逻辑都不需要改。
"""

from __future__ import annotations

import mmap
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: 空文件无法 mmap，统一用空 bytes 表示
EMPTY_DATA = b""


@dataclass(slots=True)
class Document:
    """一份待查看的数据。

    ``path`` 为 ``None`` 表示数据来自标准输入。
    """

    path: Path | None
    size: int
    _data: bytes | mmap.mmap
    _handle: BinaryIO | None = None

    @classmethod
    def open(cls, path: Path) -> Document:
        """以只读 mmap 打开文件；空文件退化为空 bytes"""
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
        """把标准输入全部读进内存（管道无法 mmap）"""
        data = sys.stdin.buffer.read()
        return cls(path=None, size=len(data), _data=data, _handle=None)

    @property
    def data(self) -> bytes | mmap.mmap:
        """底层数据，支持 ``len()`` 与切片"""
        return self._data

    @property
    def display_name(self) -> str:
        """用于提示信息的名字"""
        return str(self.path) if self.path is not None else "standard input"

    def view(self, offset: int = 0, length: int | None = None) -> memoryview:
        """取一片只读视图；mmap 下不复制任何数据"""
        view = memoryview(self._data)
        end = len(view) if length is None else offset + length
        return view[offset:end]

    def close(self) -> None:
        if isinstance(self._data, mmap.mmap) and not self._data.closed:
            self._data.close()
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None

    def __enter__(self) -> Document:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False

    # ---------- 以下为将来的编辑功能预留（V1 不实现） ----------

    def overwrite(self, offset: int, payload: bytes) -> None:
        """覆盖 ``[offset, offset + len(payload))`` 的字节（预留）"""
        raise NotImplementedError("编辑功能尚未实现（V1 只读）")

    def save(self, *, backup: bool = False) -> None:
        """把修改写回源文件（预留，计划用临时文件 + os.replace 原子写回）"""
        raise NotImplementedError("编辑功能尚未实现（V1 只读）")
