from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

from hexcontet_viewer.core import BYTES_PER_GROUP, Buffer, OCTAL_WIDTH, parse_offset, read_line
from hexcontet_viewer.document import BACKUP_SUFFIX, Document
from hexcontet_viewer.render import (
    DumpView,
    display_number,
    display_offset,
    resolve_palette,
)

#: 默认每行字节数（12 字节 = 16 个 64 进制数字 = 4 组）
DEFAULT_WIDTH = 12

#: ``--set`` 改写的永远是数据字节（中列 / Base64 侧）；右侧 ASCII 列只作预览，不能直接编辑

#: ``--set`` 值的字节写法：两位八进制数字，只能写 ``00``–``77``（即 0–63 的字节值）
VALUE_OCTAL_DIGITS = "01234567"


EPILOG = f"""\
Examples:
    viewer.py sample.bin                 View the whole file (default: {DEFAULT_WIDTH} bytes per line)
    viewer.py sample.bin -w 24 -o 0d40   Start at 0d40 (decimal 48), 24 bytes per line
    viewer.py sample.bin -o b64:64       Jump to the byte containing Base64 character 64
    viewer.py sample.bin -l 256          Show at most 256 bytes
    viewer.py -                          Read from standard input (a pipe)

Editing (in-place, the file length never changes):
    viewer.py sample.bin --set 0d27=37              Overwrite one byte (offset 31, value 0o37)
    viewer.py sample.bin --set 0d27=41_42 --dry-run  Preview without writing
    viewer.py sample.bin --set b64:16=41_42 -b       Write and keep a .bak copy
    viewer.py sample.bin --edit                      Interactive viewer (needs a terminal)

Notes:
    The line width must be a multiple of {BYTES_PER_GROUP}: one byte is 8 bits and one 64-base
    value is 6 bits, so alignment is restored only every 3 bytes (4 values = 4 Base64 chars).
    The middle and right columns match the standard Base64 encoding of the entire input.
    The offset column is duodecimal without a prefix (00000000 is offset 0, 00000010 is 12);
    the status line and the --set report write the same value with a 0d prefix, while --offset
    itself takes 0d... (standard duodecimal digits 0-9 plus a/b) or a plain decimal number.
    0x / 0o / 0b and b64:N still work; a bare hex number such as 1f no longer does.
    Every count printed by the tool (bytes, edits, file size, cell indices) is octal with 7
    written as 8; --human-compatible switches all of these numbers back to standard digits.
    The highlight is per column: the middle/Base64 cells follow the 6-bit cell, the ASCII
    column follows the byte, so editing one cell never lights up its unaligned neighbour.
    When the starting offset is not 3-byte aligned, .. marks values belonging to the previous line.
    --set values are two-digit octal bytes in 00-77 (0o00-0o77, i.e. 0-63); the offset uses the
    same syntax as --offset. --set always rewrites data bytes (the middle/Base64 side): the
    ASCII column is a preview only and cannot be edited directly.
"""


def _width_arg(text: str) -> int:
    """校验 --width：正整数且必须是 3 的倍数"""
    try:
        value = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"line width must be an integer: {text}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("每行字节数必须大于 0")
    if value % BYTES_PER_GROUP:
        raise argparse.ArgumentTypeError(
            f"每行字节数必须是 {BYTES_PER_GROUP} 的倍数（如 3、6、12、24、48）"
        )
    return value


def _count_arg(text: str) -> int:
    """校验 --length / --lines：不能为负数"""
    try:
        value = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer: {text}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return value


def _parse_value(text: str, source: str) -> bytes:
    """解析 ``--set`` 的值：两位八进制字节串（``00``–``77``）"""
    digits = text.replace(" ", "").replace("_", "")
    if not digits:
        raise argparse.ArgumentTypeError(f"empty value: {source}")
    if any(char not in VALUE_OCTAL_DIGITS for char in digits):
        raise argparse.ArgumentTypeError(
            f"value must use two-digit octal bytes 00-77: {source}"
        )
    if len(digits) % OCTAL_WIDTH:
        raise argparse.ArgumentTypeError(
            f"value needs an even number of octal digits: {source} (e.g. 01 23)"
        )
    return bytes(
        int(digits[start : start + OCTAL_WIDTH], 8)
        for start in range(0, len(digits), OCTAL_WIDTH)
    )


def _set_arg(text: str) -> tuple[int, bytes]:
    """校验 ``--set OFFSET=VALUE``：偏移复用 ``--offset`` 的语法"""
    where, separator, value = text.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(
            f"missing '=': {text} (expected OFFSET=VALUE, e.g. 0d27=37)"
        )
    try:
        offset = parse_offset(where)
    except ValueError:
        raise argparse.ArgumentTypeError(f"cannot parse offset: {where}") from None
    if offset < 0:
        raise argparse.ArgumentTypeError(f"offset cannot be negative: {where}")
    return offset, _parse_value(value, text)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器"""
    parser = argparse.ArgumentParser(
        prog="viewer.py",
        description="64-base viewer: two octal digits in the middle column and standard Base64 on the right",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        metavar="FILE",
        nargs="?",
        default="-",
        help="file to view; use - or omit it to read from standard input",
    )
    parser.add_argument(
        "-w",
        "--width",
        type=_width_arg,
        default=DEFAULT_WIDTH,
        metavar="N",
        help=f"bytes per line, must be a multiple of {BYTES_PER_GROUP} (default: {DEFAULT_WIDTH})",
    )
    parser.add_argument(
        "-o",
        "--offset",
        default="0",
        metavar="OFFSET",
        help="starting offset: 0d27 / 31 (decimal) / 0x1f / b64:16 (locate by Base64 index)",
    )
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument(
        "-l",
        "--length",
        type=_count_arg,
        default=None,
        metavar="N",
        help="show at most N bytes",
    )
    limit.add_argument(
        "-n",
        "--lines",
        type=_count_arg,
        default=None,
        metavar="N",
        help="show at most N lines (mutually exclusive with --length)",
    )
    parser.add_argument("--no-ascii", action="store_true", help="hide the ASCII column")
    parser.add_argument("--no-header", action="store_true", help="hide the header")

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--set",
        action="append",
        type=_set_arg,
        default=None,
        metavar="OFFSET=VALUE",
        help="overwrite bytes in place (repeatable), e.g. --set 0d27=41_42 --set b64:16=41_42",
    )
    action.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="open the interactive viewer (needs a terminal and a real file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --set: report what would change without touching the file",
    )
    parser.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help=f"keep a copy as <name>{BACKUP_SUFFIX} before writing",
    )
    parser.add_argument(
        "--show-changes",
        action="store_true",
        help="with --set: also dump the affected lines",
    )
    parser.add_argument(
        "--human-compatible",
        action="store_true",
        help=(
            "human-compatible mode: standard digits everywhere (octal 00-77, offset digits "
            "0-9a-b, decimal counts); default shows 7 as 8, in --edit too"
        ),
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colored output (default: auto; colorize terminals unless NO_COLOR is set)",
    )
    return parser


def _open_document(path_text: str) -> Document:
    """打开输入源；仅在失败时抛出 OSError"""
    if path_text == "-":
        return Document.from_stdin()
    path = Path(path_text)
    if not path.is_file():
        raise OSError(f"not a regular file: {path}")
    return Document.open(path)


def _make_output_robust() -> None:
    """输出被重定向到不支持中文的代码页时，用 ? 代替而不是直接抛异常。

    终端（isatty）由 Python 固定用 UTF-8 写控制台，不需要处理。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or stream.isatty():
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 输出被替换时的兜底
            pass


def _handle_broken_pipe() -> int:
    """下游（如 head）提前关闭管道时静默退出，不打印异常"""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:  # pragma: no cover - 极少数环境无法替换 fd
        pass
    return 0


def _affected_line_offsets(view: DumpView, offsets: Iterable[int]) -> list[int]:
    """把字节偏移换算成视图里的行首偏移（去重、排序，忽略视图窗口之前的行）"""
    starts = set()
    for offset in offsets:
        if offset < view.offset:
            continue
        starts.add(view.offset + ((offset - view.offset) // view.width) * view.width)
    return sorted(starts)


def _print_changed_lines(view: DumpView, data: Buffer, offsets: Iterable[int]) -> None:
    """把受影响的行（改写后的状态）连同表头一起打印到标准输出"""
    starts = _affected_line_offsets(view, offsets)
    if not starts:
        return
    for text in view.header_lines():
        print(text)
    for start in starts:
        print(view.line(read_line(data, start, view.width)))


def _shown_count(value: int, human_compatible: bool) -> str:
    """报出来的数量：默认八进制（把 ``7`` 写成 ``8``），``--human-compatible`` 时十进制"""
    return display_number(value, human_compatible=human_compatible)


def _run_patch(document: Document, view: DumpView, args: argparse.Namespace) -> int:
    """执行 ``--set``：先改写内存数据，再按需预览、写盘"""
    edits = []
    for offset, payload in args.set:
        if offset + len(payload) > document.size:
            # 自己报错：这里的数量要跟着 --human-compatible 走，document 层不知道这个开关
            where = display_offset(
                offset, 1, human_compatible=view.human_compatible
            )
            print(
                f"error: offset {where}"
                f" + {_shown_count(len(payload), view.human_compatible)} bytes exceeds"
                f" {document.display_name}"
                f" ({_shown_count(document.size, view.human_compatible)} bytes)",
                file=sys.stderr,
            )
            return 1
        try:
            edits.append(document.overwrite(offset, payload))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    changes = [change for edit in edits for change in edit.byte_changes()]
    for offset, before, after in changes:
        # 偏移用与转储一致的十二进制写法；字节值本身是八进制（与 --set 的输入写法一致）
        where = display_offset(
            offset, view.offset_width, human_compatible=view.human_compatible
        )
        print(f"{where}: {before:02o} -> {after:02o}")

    if not changes:
        print(
            f"no change: {document.display_name} already contains the requested bytes",
            file=sys.stderr,
        )
        return 0

    if args.show_changes:
        _print_changed_lines(
            view.with_changes(document.changed_offsets, document.changed_cells),
            document.data,
            (offset for offset, _before, _after in changes),
        )

    if args.dry_run:
        count = _shown_count(len(changes), view.human_compatible)
        print(
            f"dry run: {count} byte(s) in {document.display_name} left untouched",
            file=sys.stderr,
        )
        return 0

    try:
        document.save(backup=args.backup)
    except OSError as error:
        print(f"error: cannot save {document.display_name}: {error}", file=sys.stderr)
        return 1

    written = (
        f"wrote {_shown_count(len(changes), view.human_compatible)}"
        f" byte(s) to {document.display_name}"
    )
    if args.backup and document.path is not None:
        written += f" (backup: {document.path.name}{BACKUP_SUFFIX})"
    print(written, file=sys.stderr)
    return 0


def _run_viewer(document: Document, offset: int, args: argparse.Namespace) -> int:
    """启动交互式编辑器"""
    if not document.writable:
        print("error: --edit needs a file path; standard input cannot be written back", file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("error: --edit needs an interactive terminal", file=sys.stderr)
        return 1

    from hexcontet_viewer.tui import run_tui  # 延迟导入：普通查看用不到终端支持

    return run_tui(
        document,
        width=args.width,
        offset=offset,
        backup=args.backup,
        show_ascii=not args.no_ascii,
        human_compatible=args.human_compatible,
    )


def main(argv: list[str] | None = None) -> int:
    """命令行主流程；返回进程退出码"""
    parser = build_parser()
    args = parser.parse_args(argv)
    _make_output_robust()

    try:
        offset = parse_offset(args.offset)
    except ValueError:
        parser.error(f"cannot parse offset: {args.offset}")

    if args.dry_run and not args.set:
        parser.error("--dry-run only makes sense together with --set")

    # 先拦住标准输入：否则会先去读 stdin 卡住，而不是给出可读的报错
    if args.path == "-" and (args.edit or args.set):
        flag = "--edit" if args.edit else "--set"
        print(
            f"error: {flag} needs a file path; standard input cannot be written back",
            file=sys.stderr,
        )
        return 1

    try:
        document = _open_document(args.path)
    except OSError as error:
        print(f"error: cannot read {args.path}: {error}", file=sys.stderr)
        return 1

    with document:
        if offset > document.size:
            where = display_offset(offset, 1, human_compatible=args.human_compatible)
            total = _shown_count(document.size, args.human_compatible)
            print(
                f"error: offset {where} ({_shown_count(offset, args.human_compatible)})"
                f" exceeds {document.display_name} ({total} bytes)",
                file=sys.stderr,
            )
            return 1

        if args.edit:
            return _run_viewer(document, offset, args)

        if offset % BYTES_PER_GROUP:
            print("note: the starting offset is not 3-byte aligned; .. marks values belonging to the previous line", file=sys.stderr)

        view = DumpView.create(
            document.size,
            width=args.width,
            offset=offset,
            palette=resolve_palette(args.color),
            show_ascii=not args.no_ascii,
            human_compatible=args.human_compatible,
        )

        if args.set:
            return _run_patch(document, view, args)

        available = document.size - offset
        shown = available
        if args.length is not None:
            shown = min(shown, args.length)
        if args.lines is not None:
            shown = min(shown, args.lines * args.width)

        try:
            for text in view.render(
                document.data,
                header=not args.no_header,
                max_bytes=args.length,
                max_lines=args.lines,
            ):
                sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return _handle_broken_pipe()

    if shown < available:
        omitted = _shown_count(available - shown, args.human_compatible)
        total = _shown_count(document.size, args.human_compatible)
        print(
            f"omitted {omitted} bytes"
            f" ({document.display_name}: {total} bytes total)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
