# hextetra-viewer

64 进制文件查看器。像 hexdump 一样逐行查看文件，但**中间列是 64 进制数字**，**右栏是对应的标准 Base64**（RFC 4648）。

零依赖，只用 Python 标准库；数据全程本地读取，不上传任何内容。

## 为什么是“64 进制 + 两位八进制”

| 单位 | 位数 | 说明 |
| -- | -- | -- |
| 字节 | 8 bit | 传统 hexdump 的单位 |
| 64 进制数字 | 6 bit | 取值范围 0–63 |
| 3 字节 | 24 bit | = 4 个 64 进制数字 = 4 个 Base64 字符（**最小对齐单位**） |

- 64 进制数字 0–63 正好写成两位八进制（`63₁₀` = `88`），所以中间列不出现十六进制字母；
- `--human-compatible` 可将显示转为 `00`–`77` 形式。
- 字节与 64 进制数字不对齐，所以**每行字节数必须是 3 的倍数**（默认 12）；
- 中间列与右栏都严格对应**整份数据的标准 Base64**，可以直接和 `base64` 命令的输出逐字符比对；
- 起始偏移没有按 3 字节对齐时，行首用 `..` 表示“该数字归属上一行”；
- 数据结尾不足 3 字节时，中间列写 `--`、右栏写 `=`（Base64 的补位）。

以 `'ABC'`（`41 42 43`）为例：

```
字节:   01000001 01000010 01000011
6 bit:  010000   010100   001001   000011
八进制:   20       24       11       03
Base64:    Q        U        J        D     →  "QUJD"
```

## 输出示例

对 `b"ABCABCABCABCABCDEFGH"`（20 字节）运行：

```bash
uv run python viewer.py sample.bin
```

```text
# 中间列：两位八进制 = 1 个 64 进制数字；4 个数字 = 3 字节 = 4 个 Base64 字符
偏移      中间列                                              |  Base64                  |  ASCII
00000000  20 24 11 03  20 24 11 03  20 24 11 03  20 24 11 03  |  QUJD  QUJD  QUJD  QUJD  |  ABCABCABCABC
0000000c  20 24 11 03  21 04 25 06  21 64 40 --               |  QUJD  REVG  R0g=        |  ABCDEFGH
```

第二行的整行 Base64 就是 `QUJDREVGR0g=`，与 `base64` 命令输出一致。
中间列与右栏按 3 字节分组一一对应（中间一组 4 个数字、右栏一组 4 个字符），
两列字符宽度不同（2 vs 1），因此只保证分组对应、列首对齐，不做逐字符对齐。

## 快速开始

安装（本项目零依赖，`uv sync` 只是建虚拟环境）：

```bash
uv sync
```

查看文件 / 标准输入：

```bash
uv run python viewer.py sample.bin                 # 默认每行 12 字节
uv run python viewer.py sample.bin -w 24           # 每行 24 字节
uv run python viewer.py sample.bin -o 0x30 -n 4    # 从 0x30 起看 4 行
uv run python viewer.py sample.bin -o b64:64       # 跳到 Base64 第 64 个字符所在的字节
Get-Content sample.bin -AsByteStream | uv run python viewer.py -
```

## 参数

| 参数 | 默认 | 说明 |
| -- | -- | -- |
| `FILE` | `-` | 文件路径；`-` 或省略表示从标准输入读 |
| `-w`, `--width N` | `12` | 每行字节数，必须是 3 的倍数（3/6/12/24/48） |
| `-o`, `--offset` | `0` | 起始偏移：`0x1f` / `1f` / `31` / `0o17` / `b64:16` |
| `-l`, `--length N` | 全部 | 最多显示 N 字节 |
| `-n`, `--lines N` | 全部 | 最多显示 N 行（与 `--length` 互斥） |
| `--no-ascii` | 关 | 不显示右侧 ASCII 列 |
| `--no-header` | 关 | 不显示表头 |
| `--human-compatible` | 关 | 将 `8` 显示成人类可读的 `7` |
| `--color` | `auto` | `auto` / `always` / `never`；`auto` 在标准输出是终端且未设置 `NO_COLOR` 时着色 |

退出码：正常 `0`；文件不存在 / 偏移越界 `1`；参数非法 `2`。

## 目录结构

```text
hextetra-viewer/
├── pyproject.toml            # requires-python >=3.12，零依赖，[tool.uv] package = false
├── viewer.py                 # 命令行入口：参数解析 + 输出
├── hextetra_viewer/
│   ├── core.py               # 纯算法：字节 ⇄ 6 bit 单元，偏移解析
│   ├── render.py             # 列宽计算、行渲染、ANSI 配色
│   └── document.py           # 数据源：文件 mmap / 标准输入（预留编辑接缝）
└── tests/
    ├── test_core.py          # 算法与标准库 base64 对拍
    └── test_render.py        # 列对齐、补位写法、截断、配色
```

分层约定：`core` 不碰 I/O 与显示，`render` 不碰 I/O，`document` 只管数据来源，
所以将来加交互界面或编辑功能都不需要重写算法。

## 测试

零依赖，标准库 `unittest`：

```bash
uv run python -m unittest
```

覆盖范围：

- 右栏与 `base64.b64encode` 对拍（长度 0–100、行宽 3/6/12/24/48）
- 每个 64 进制数字只被显示一次、不重不漏；未对齐偏移下仍与整份 Base64 对齐
- 末行补位（`--` / `=`）、空文件、单字节、越界、`--length` 截断
- 表头与数据行的分隔符列号逐行一致（全角字符按 2 列计算）
- 着色开关不影响列宽，`NO_COLOR` 生效

## 后续：编辑功能

`document.Document` 已经预留好接缝，目前调用会抛 `NotImplementedError`：

```python
document.overwrite(offset, payload)   # 覆盖一段字节
document.save(backup=True)            # 临时文件 + os.replace 原子写回
```

计划中的实现路线：

1. 只读 mmap 换成可写的 `bytearray` 后端，加脏标记与撤销栈；
2. 命令行 `viewer.py patch FILE --set 0x1f=0xff --dry-run` 之类的一次性改写；
3. 需要交互时再补 `tui.py`，渲染层只要加一个“高亮区间”参数即可复用。

## 已知取舍

- 不做流式/增量读取：文件走只读 mmap（超大文件也不吃内存），标准输入会一次性读入内存；
- ASCII 列只把 `0x20`–`0x7E` 原样显示，其余字节（含多字节编码）统一显示为 `.`；
- 非 ASCII 终端或未设置 UTF-8 的输出环境里，表头里的中文可能显示不全，但不影响数据列；
- 超长行建议配合分页器使用，例如 `viewer.py big.bin | less -R`（`-R` 保留颜色）。
