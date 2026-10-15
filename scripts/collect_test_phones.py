"""从 ``json_by_phone/`` 提取测试手机号清单。

背景：测试方按「一个号码一份 json」交付，文件名即手机号，但目录由解压产生，混有
macOS 的 ``__MACOSX/`` 与 ``._`` 前缀残留，且两批的目录层级不同（A 批多套了一层
``json_by_phone/``）。本脚本只负责稳健地挑出真实数据文件、抽出号码、去重排序，
落成一份可供长期复用的清单——**不读 json 正文**，正文含真实客户标签，不在本脚本职责内。

三类输入的处理口径（**不静默丢弃**）：

- ``matched``：``.json`` 且文件名是合法手机号 —— 采用。
- ``junk``：路径含 ``__MACOSX`` 或以 ``._`` 开头 —— 预期内的解压残留，只计数。
- ``anomalies``：``.json`` 但文件名不是合法手机号 —— **异常**，打印出来让人处理，
  因为这意味着交付物里混了非预期文件；静默跳过会让清单悄悄少号码。

用法：

    python scripts/collect_test_phones.py           # 生成清单
    python scripts/collect_test_phones.py --check   # 只校验清单与目录是否一致
    python scripts/collect_test_phones.py --json    # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ROOT = REPO_ROOT / "json_by_phone"
DEFAULT_OUT = REPO_ROOT / "test_phone_numbers.txt"

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
"""与 ``api.py`` 的主入口入参校验同形，不一致的号码提交时会被 422 拒掉。"""

JUNK_DIR = "__MACOSX"
JUNK_PREFIX = "._"


def is_junk(path: Path) -> bool:
    """是否为 macOS 解压残留（预期内的噪声，不是异常）。"""
    if path.name.startswith(JUNK_PREFIX):
        return True
    return any(part == JUNK_DIR for part in path.parts)


def iter_phone_files(root: Path) -> Iterator[Path]:
    """递归遍历目录下的全部文件。

    必须递归：A 批是 ``json_by_phone_A/json_by_phone/*.json``，B 批是
    ``json_by_phone_B/*.json``，层级不同，写死层数会漏掉一批。
    """
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


@dataclass
class ScanReport:
    """一次扫描的结果。异常单独列出，便于人介入。"""

    matched: list[str] = field(default_factory=list)
    junk: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "matched_count": len(self.matched),
            "junk": self.junk,
            "junk_count": len(self.junk),
            "anomalies": self.anomalies,
            "anomaly_count": len(self.anomalies),
            "duplicates": self.duplicates,
            "duplicate_count": len(self.duplicates),
        }


def scan(root: Path) -> ScanReport:
    """扫描目录，返回去重升序后的号码清单与三类计数。"""
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")

    report = ScanReport()
    seen: set[str] = set()

    for path in iter_phone_files(root):
        rel = str(path.relative_to(root))
        if is_junk(path):
            report.junk.append(rel)
            continue
        if path.suffix.lower() != ".json":
            report.anomalies.append(f"{rel}（非 .json）")
            continue
        stem = path.stem
        if not PHONE_RE.fullmatch(stem):
            report.anomalies.append(f"{rel}（文件名不是合法手机号）")
            continue
        if stem in seen:
            report.duplicates.append(rel)
            continue
        seen.add(stem)
        report.matched.append(stem)

    report.matched.sort()
    return report


def read_phone_list(path: Path) -> list[str]:
    """读取清单：忽略空行与 ``#`` 注释，保留出现顺序（不排序，由调用方决定）。"""
    if not path.is_file():
        raise FileNotFoundError(f"清单不存在：{path}")
    phones: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        phones.append(candidate)
    return phones


def write_phone_list(path: Path, phones: Sequence[str]) -> int:
    """写入清单：UTF-8、LF 换行、结尾带换行。返回字节数。

    显式指定编码与换行，不依赖平台默认——本仓有中文编码异常的历史，落盘一律显式。
    """
    body = "\n".join(phones) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def _print_summary(report: ScanReport) -> None:
    """stdout 只打 ASCII 进度行；中文进文件，避免控制台代码页把输出打乱。"""
    print(f"matched   : {len(report.matched)}")
    print(f"junk      : {len(report.junk)}  (macOS __MACOSX / ._ residue)")
    print(f"anomalies : {len(report.anomalies)}")
    print(f"duplicates: {len(report.duplicates)}")
    for item in report.anomalies:
        print(f"  [anomaly] {item}")


def _check(root: Path, out: Path, as_json: bool) -> int:
    """校验清单与目录是否一致。不一致返回 1，供跑前自检与 CI 使用。"""
    report = scan(root)
    if not out.is_file():
        print(f"missing list file: {out}")
        return 1
    listed = read_phone_list(out)

    problems: list[str] = []
    if len(listed) != len(set(listed)):
        problems.append("list contains duplicates")
    if listed != sorted(listed):
        problems.append("list is not sorted ascending")
    missing = sorted(set(report.matched) - set(listed))
    extra = sorted(set(listed) - set(report.matched))
    if missing:
        problems.append(f"missing from list: {missing[:5]}")
    if extra:
        problems.append(f"not in directory: {extra[:5]}")
    if report.anomalies:
        problems.append(f"anomalies in directory: {len(report.anomalies)}")

    payload = {
        "root": str(root),
        "out": str(out),
        "listed_count": len(listed),
        "scanned_count": len(report.matched),
        "missing": missing,
        "extra": extra,
        "problems": problems,
        "ok": not problems,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        print(f"listed   : {len(listed)}")
        print(f"scanned  : {len(report.matched)}")
        if problems:
            print("PROBLEMS:")
            for item in problems:
                print(f"  - {item}")
        else:
            print("OK: list matches directory")
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取测试手机号清单")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="扫描根目录")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="清单落地路径")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    if args.check:
        return _check(args.root, args.out, args.json)

    report = scan(args.root)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
    else:
        _print_summary(report)

    if report.anomalies:
        print("refusing to write: resolve anomalies first", file=sys.stderr)
        return 1

    size = write_phone_list(args.out, report.matched)
    if not args.json:
        print(f"wrote {len(report.matched)} phones ({size} bytes) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
