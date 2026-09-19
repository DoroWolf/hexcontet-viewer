"""64 进制查看器（hextetra-viewer）的命令行入口。

用法示例：

    uv run python viewer.py sample.bin              # 默认每行 12 字节
    uv run python viewer.py sample.bin -w 24        # 每行 24 字节
    uv run python viewer.py sample.bin -o b64:64    # 跳到 Base64 第 64 个字符
    Get-Content sample.bin -AsByteStream | uv run python viewer.py - 
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hextetra_viewer.core import BYTES_PER_GROUP, parse_offset
from hextetra_viewer.document import Document
from hextetra_viewer.render import DumpView, resolve_palette

#: 默认每行字节数（12 字节 = 16 个 64 进制数字 = 4 组）
DEFAULT_WIDTH = 12


EPILOG = f"""\
Examples:
    viewer.py sample.bin                 View the whole file (default: {DEFAULT_WIDTH} bytes per line)
    viewer.py sample.bin -w 24 -o 0x30   Start at 0x30, with 24 bytes per line
    viewer.py sample.bin -o b64:64       Jump to the byte containing Base64 character 64
    viewer.py sample.bin -l 256          Show at most 256 bytes
    viewer.py -                          Read from standard input (a pipe)

Notes:
    The line width must be a multiple of {BYTES_PER_GROUP}: one byte is 8 bits and one 64-base
    value is 6 bits, so alignment is restored only every 3 bytes (4 values = 4 Base64 chars).
    The middle and right columns match the standard Base64 encoding of the entire input.
    When the starting offset is not 3-byte aligned, .. marks values belonging to the previous line.
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
        help="starting offset: 0x1f / 1f / 31 / b64:16 (locate by Base64 character index)",
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
    parser.add_argument(
        "--human-compatible",
        action="store_true",
        help="human-compatible mode: restore the standard offset alphabet and preserve middle-column 7s",
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


def main(argv: list[str] | None = None) -> int:
    """命令行主流程；返回进程退出码"""
    parser = build_parser()
    args = parser.parse_args(argv)
    _make_output_robust()

    try:
        offset = parse_offset(args.offset)
    except ValueError:
        parser.error(f"cannot parse offset: {args.offset}")

    try:
        document = _open_document(args.path)
    except OSError as error:
        print(f"error: cannot read {args.path}: {error}", file=sys.stderr)
        return 1

    with document:
        if offset > document.size:
            print(f"error: offset 0x{offset:x} ({offset}) exceeds {document.display_name} ({document.size} bytes)", file=sys.stderr)
            return 1

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
        print(f"omitted {available - shown} bytes ({document.display_name}: {document.size} bytes total)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
