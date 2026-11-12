#!/usr/bin/env python
"""跑 MySQL 后端契约测试，DSN 从 ``.env`` 现取。

**为什么需要这个脚本。** 把 DSN 直接写在命令行里，密码会进 shell 历史、进进程列表、
进任何记录命令的地方。而 ``.env`` 本来就不入库、只在本机，从它派生是最短的可信路径。

用法：

    python scripts/run_mysql_contract_tests.py
    python scripts/run_mysql_contract_tests.py -k claim -v

它会：

1. 读仓库根 ``.env`` 的 ``MYSQL_HOST`` / ``MYSQL_PORT`` / ``MYSQL_USER`` /
   ``MYSQL_PASSWORD`` / ``MYSQL_DATABASE``；
2. 拼出 ``mysql://…`` 形式的 DSN，**写入子进程的环境变量**（不落盘、不回显）；
3. 以 ``CUSTOMER_PROFILE_MYSQL_URL`` 调 ``pytest tests/test_mysql_backend.py``。

**只用于开发/联调。** UAT 与生产不跑这条；那边的验证是「服务起来后能正常接单」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
DEFAULT_SCHEMA = "customer_profile_runtime"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def main(argv: list[str]) -> int:
    env = read_env(ENV_FILE)
    host = env.get("MYSQL_HOST", "").strip()
    user = env.get("MYSQL_USER", "").strip()
    password = env.get("MYSQL_PASSWORD", "")
    if not host or not user:
        print("缺少 MYSQL_HOST / MYSQL_USER，无法连接", file=sys.stderr)
        return 2

    port = env.get("MYSQL_PORT", "3306").strip() or "3306"
    # 契约测试会清表，必须指向专用 schema。.env 里的 MYSQL_DATABASE 是业务库，
    # 所以这里固定用运行时库，并允许用 --schema 覆盖。
    schema = DEFAULT_SCHEMA
    if "--schema" in argv:
        index = argv.index("--schema")
        schema = argv[index + 1]
        argv = argv[:index] + argv[index + 2 :]

    from urllib.parse import quote

    dsn = (
        f"mysql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{schema}?charset=utf8mb4"
    )

    child_env = dict(os.environ)
    child_env["CUSTOMER_PROFILE_MYSQL_URL"] = dsn
    command = [sys.executable, "-m", "pytest", "tests/test_mysql_backend.py", *argv]
    print(f"运行 MySQL 契约测试：{user}@{host}:{port}/{schema}", flush=True)
    return subprocess.call(command, cwd=REPO_ROOT, env=child_env)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
