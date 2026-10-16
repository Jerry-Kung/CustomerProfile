"""生成 ``INBOUND_API_KEY``——对外任务接口（8099）的入站密钥。

为什么需要脚本而不只是「随手敲个随机串」：这个值要在**两端**填成同一个——
我们的 ``.env`` 与前端任务队列的配置。手敲的随机串有三个常见问题：太短；字符被输入法
或复制悄悄改写（``l``/``1``、``O``/``0``、大小写）；两端各抄一半后无法核对。这里把
生成、格式固定与落盘做成一条命令，产出的值可以直接粘给前端。

三条口径：

1. **默认只打印，不动任何文件。** ``.env`` 存真实凭据，写它必须是显式动作（``--write``）。
2. **不覆盖已有的非空值**，除非显式 ``--force``。换密钥是一次需要双方同时动作的运维事件
   ——对端还拿着旧值时，它的请求会全部 403。这件事不该由一次手滑完成。
3. **不回显旧值。** 脚本只判断「这个键有没有值」，不打印它；``.env`` 已被 gitignore，
   不要把生成的值拷进任何受版本控制的文件。

用法：

    python scripts/gen_inbound_key.py                   # 打印一个新密钥
    python scripts/gen_inbound_key.py --write           # 写入 .env（已有非空值时拒绝）
    python scripts/gen_inbound_key.py --write --force   # 换密钥：覆盖已有值
    python scripts/gen_inbound_key.py --json            # 机器可读输出

stdout 保持 ASCII：控制台代码页不是 UTF-8 时中文提示会乱码，而密钥本身是 ASCII，
所以面向人的提示也一律用 ASCII，避免在复制密钥时混入乱码行。
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

ENV_NAME = "INBOUND_API_KEY"
DEFAULT_PREFIX = "inb_"
DEFAULT_BYTES = 32
"""默认 32 字节 = 256 位随机熵，十六进制后 64 字符。"""

MIN_BYTES = 16
"""下限 16 字节 = 128 位。再短就不该用来挡一个会产生真实算力与生产回写的入口。"""

MAX_BYTES = 64
"""上限防止把一行 ``.env`` 写到离谱的长度（64 字节即 512 位，已远超需要）。"""


class KeyAlreadySet(RuntimeError):
    """目标键已有非空值，且未指定 ``--force``。**异常文本不包含旧值。**"""


def key_pattern(nbytes: int = DEFAULT_BYTES, prefix: str = DEFAULT_PREFIX):
    """按参数返回密钥的校验正则，供本脚本与用例共用同一份形状定义。"""
    return re.compile(rf"^{re.escape(prefix)}[0-9a-f]{{{nbytes * 2}}}$")


def generate_key(nbytes: int = DEFAULT_BYTES, prefix: str = DEFAULT_PREFIX) -> str:
    """生成一个密钥：``前缀 + 随机十六进制``。

    用十六进制而不是 base64：这个值要在 ``.env``、shell 的 ``-H`` 参数、前端配置三处
    各出现一次，base64 的大小写与 ``+``/``/`` 在手工传递时容易被改写，hex 不会。同样
    位宽下字符串略长，但省掉了一整类「两端只差一个字符」的联调故障。
    """
    if nbytes < MIN_BYTES:
        raise ValueError(f"字节数至少 {MIN_BYTES}，实际为 {nbytes}")
    if nbytes > MAX_BYTES:
        raise ValueError(f"字节数至多 {MAX_BYTES}，实际为 {nbytes}")
    return f"{prefix}{secrets.token_hex(nbytes)}"


def _split_line_ending(line: str) -> tuple[str, str]:
    """把一行拆成「正文」与「行尾符」，写入时按原样还原。

    ``.env`` 可能是 CRLF（本仓在 Windows 上开发）。整份重写时若把行尾统一成 LF，
    每次改密钥都会产生一次无意义的整文件 diff。
    """
    body = line.rstrip("\r\n")
    return body, line[len(body) :]


def _line_key(body: str) -> str | None:
    """取 ``KEY=...`` 行的键名（大写归一）；注释行与无等号行返回 ``None``。"""
    if body.lstrip().startswith("#"):
        return None
    name, sep, _ = body.partition("=")
    if not sep:
        return None
    return name.strip().upper()


def read_env_key(env_path: Path, name: str = ENV_NAME) -> str | None:
    """读取该键当前的值；文件不存在、无该键、或值为空都返回 ``None``。

    只用于「是否已有值」的判断。调用方**不得**把返回值打印或落进日志。

    读时显式 ``newline=""``：文本模式的通用换行会把 ``\\r\\n`` 折成 ``\\n``，那样
    ``write_env_key`` 回写时就保不住原有行尾（Windows 上的 ``.env`` 常见 CRLF），
    每次改密钥都会留下一整份无意义的 diff。
    """
    if not env_path.is_file():
        return None
    with env_path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    for line in text.splitlines():
        body, _ = _split_line_ending(line)
        if _line_key(body) == name.upper():
            # 空值按「没有值」处理：``INBOUND_API_KEY=`` 是模板里的占位行，
            # 把它当作「已设置」会让第一次 ``--write`` 就被拒绝。
            return body.partition("=")[2].strip() or None
    return None


def write_env_key(
    env_path: Path,
    key: str,
    *,
    name: str = ENV_NAME,
    force: bool = False,
) -> str:
    """把密钥写进 ``.env``，返回实际动作：``"created"`` / ``"updated"`` / ``"appended"``。

    只改这一个键那一行，其余行按原字节回写（含行尾符），因此不会产出与本次改动无关的 diff。
    ``.env`` 不存在时创建一个只含该键的文件（此时多半配置还没起步，创建比报错更有用）。
    """
    if read_env_key(env_path, name) and not force:
        raise KeyAlreadySet(
            f"{name} already has a value in {env_path}; "
            "refusing to overwrite it (the peer still holds the old key)"
        )

    if not env_path.is_file():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        with env_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(f"{name}={key}\n")
        return "created"

    # ``newline=""`` 读入：保留行尾原样，才能在下文按原行尾回写。
    with env_path.open("r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines(keepends=True)
    out: list[str] = []
    replaced = False
    for line in lines:
        body, ending = _split_line_ending(line)
        if not replaced and _line_key(body) == name.upper():
            out.append(f"{name}={key}{ending}")
            replaced = True
        else:
            out.append(line)

    if replaced:
        action = "updated"
    else:
        # 追加前确保上一行已收尾，否则新键会被粘到最后一个值后面。
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += "\n"
        out.append(f"{name}={key}\n")
        action = "appended"

    # ``newline=""``：不让 Python 把行尾翻译一次，原样落盘。
    with env_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("".join(out))
    return action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate INBOUND_API_KEY")
    parser.add_argument(
        "--bytes",
        type=int,
        default=DEFAULT_BYTES,
        dest="nbytes",
        help=f"random bytes (default {DEFAULT_BYTES} = {DEFAULT_BYTES * 2} hex chars)",
    )
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="key prefix")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="target env file (default: repository .env)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write into the env file instead of only printing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing non-empty value (key rotation)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if args.force and not args.write:
        parser.error("--force only makes sense together with --write")

    try:
        key = generate_key(args.nbytes, args.prefix)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, object] = {
        "key": key,
        "length": len(key),
        "env_name": ENV_NAME,
        "env_file": str(args.env_file),
        "written": None,
    }

    if args.write:
        try:
            payload["written"] = write_env_key(args.env_file, key, force=args.force)
        except KeyAlreadySet:
            print(
                f"refused: {ENV_NAME} is already set in {args.env_file}",
                file=sys.stderr,
            )
            print(
                "pass --force to rotate; update the frontend task queue at the same time",
                file=sys.stderr,
            )
            return 1
        except OSError as exc:
            print(f"error: cannot write {args.env_file}: {exc}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(key)
        if payload["written"]:
            print(f"written: {payload['written']} -> {args.env_file}")
        print("give this value to the frontend task queue, then restart the inbound process")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
