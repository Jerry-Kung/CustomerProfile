# -*- coding: utf-8 -*-
"""把 index.html + app.js + data/payload.json 打包成单文件自包含页面。

用法（在本目录下执行）：

    python build.py

产出：``../画像管线AB对照-v3.html`` —— 数据内联，双击即可打开，无需起本地服务。

为什么单文件：数据含真实客户姓名、手机号与录音转写，页面不宜依赖同目录 JSON 才能打开，
也不宜发布到任何在线地址；合成一个文件便于本地留存与私下传阅。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "画像管线AB对照-v3.html"

BLOCKS = [("data/payload.json", "d-payload")]


def _embed(path: Path, element_id: str) -> str:
    """把一个 JSON 文件转成内联数据块。

    转义说明：``&`` ``<`` ``>`` 一律写成 ``\\u00XX``。HTML 解析器在 ``<script>`` 内部只认
    ``</script`` 与 ``<!--`` 作为终结符，转义后即使在字符串里出现也不会提前闭合；
    而 ``\\u003c`` 是合法 JSON 转义，``JSON.parse`` 会原样还原。
    """
    obj = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return '<script type="application/json" id="%s">%s</script>' % (element_id, text)


def main() -> int:
    html = (HERE / "index.html").read_text(encoding="utf-8")
    app = (HERE / "app.js").read_text(encoding="utf-8")

    for bad in ("</script", "<script", "<!--"):
        if re.search(re.escape(bad), app, re.I):
            print("错误：app.js 含 %s，无法安全内联" % bad, file=sys.stderr)
            return 1

    marker = '<script src="app.js"></script>'
    if marker not in html:
        print("错误：index.html 未找到 %s" % marker, file=sys.stderr)
        return 1

    missing = [p for p, _ in BLOCKS if not (HERE / p).exists()]
    if missing:
        print("错误：缺少数据文件 %s" % "、".join(missing), file=sys.stderr)
        return 1

    blocks = "\n".join(_embed(HERE / p, eid) for p, eid in BLOCKS)
    out = html.replace(marker, blocks + "\n<script>\r\n" + app + "\r\n</scr" + "ipt>")
    OUT.write_text(out, encoding="utf-8")

    size = OUT.stat().st_size
    print("已生成 %s" % OUT)
    print("  %s 字节 ≈ %.2f MB" % (format(size, ","), size / 1048576))
    for p, eid in BLOCKS:
        print("  内联 %-24s -> id=%s" % (p, eid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
