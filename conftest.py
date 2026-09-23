"""把 ``src/`` 加入导入路径。

本仓用 src 布局但未做可安装打包（V0.2 不需要 ``pip install -e .``）。pytest 从仓库
根运行，这里补上路径即可，避免要求使用者先激活虚拟环境再装包。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
