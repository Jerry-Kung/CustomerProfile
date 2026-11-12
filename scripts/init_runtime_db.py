#!/usr/bin/env python
"""运行时库初始化脚本（宿主机入口）。

逻辑在 ``customer_profile.persistence.init_db`` 里——镜像只带 ``src/``，
容器内执行的是 ``python -m customer_profile.persistence.init_db``。本文件只是
让宿主机上也能直接跑同一个入口（无需先 ``pip install``）。

```bash
python scripts/init_runtime_db.py --dry-run                 # 看会执行什么
python scripts/init_runtime_db.py                           # 正式执行
python scripts/init_runtime_db.py --emit-sql db/init.sql    # 导出纯 SQL 交给 DBA
```

约束（与模块文档一致）：幂等、不写死地址、不假设能改库级字符集、执行完可核对。
它**不**迁数据（见 ``scripts/migrate_sqlite_to_mysql.py``）、不建账号、不删任何东西。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

if __name__ == "__main__":
    from customer_profile.persistence.init_db import main

    raise SystemExit(main(sys.argv[1:]))
