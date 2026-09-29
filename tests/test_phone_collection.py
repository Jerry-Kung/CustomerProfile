"""测试手机号清单：提取、去重、清单读写与漂移校验。

纯文件系统用例（``tmp_path``），不碰数据库也不出网——本文件的被测对象是一个遍历目录
的纯函数，没有需要服务的地方。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import collect_test_phones as ctp  # noqa: E402

COLLECT_SCRIPT = REPO_ROOT / "scripts" / "collect_test_phones.py"
REAL_LIST = REPO_ROOT / "test_phone_numbers.txt"
REAL_ROOT = REPO_ROOT / "json_by_phone"


def _tree(root, spec: dict[str, str]) -> None:
    """按 ``相对路径 -> 内容`` 铺一个目录树。"""
    for rel, body in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def test_recurses_into_both_batch_shapes(tmp_path):
    """A 批多套一层目录、B 批是平的——两批都要命中。

    这条挡的是「写死层数导致静默漏掉一整批」：只扫一层会拿到 0 个，不报错。
    """
    _tree(
        tmp_path,
        {
            "json_by_phone_A/json_by_phone/13415100087.json": "{}",
            "json_by_phone_A/json_by_phone/13527911093.json": "{}",
            "json_by_phone_B/13430349943.json": "{}",
            "json_by_phone_B/13719210972.json": "{}",
        },
    )
    report = ctp.scan(tmp_path)
    assert report.matched == [
        "13415100087",
        "13430349943",
        "13527911093",
        "13719210972",
    ]
    assert report.anomalies == []


def test_excludes_macos_junk(tmp_path):
    """``__MACOSX/`` 下的 ``._`` 残留是预期噪声，计入 junk 而非 anomalies。"""
    _tree(
        tmp_path,
        {
            "A/__MACOSX/json_by_phone/._13415100087.json": "x",
            "A/__MACOSX/._A": "x",
            "A/._13415100087.json": "x",
            "A/13415100087.json": "{}",
        },
    )
    report = ctp.scan(tmp_path)
    assert report.matched == ["13415100087"]
    assert len(report.junk) == 3
    assert report.anomalies == []


def test_json_with_bad_name_is_an_anomaly_not_silently_dropped(tmp_path):
    """``.json`` 但文件名不是合法手机号 —— 必须报出来。

    静默跳过会让清单悄悄少号码，而「少号码」与「号码本身不需要跑」在结果上无法区分。
    """
    _tree(
        tmp_path,
        {
            "A/13415100087.json": "{}",
            "A/notes.json": "{}",
            "A/1234567890.json": "{}",  # 10 位
            "A/23415100087.json": "{}",  # 不以 1 开头
            "A/1a415100087.json": "{}",  # 含字母
        },
    )
    report = ctp.scan(tmp_path)
    assert report.matched == ["13415100087"]
    assert len(report.anomalies) == 4


def test_non_json_files_are_anomalies(tmp_path):
    """非 ``.json`` 文件也算异常：交付物里多出别的东西值得人看一眼。"""
    _tree(tmp_path, {"A/13415100087.json": "{}", "A/README.md": "hi"})
    report = ctp.scan(tmp_path)
    assert report.matched == ["13415100087"]
    assert len(report.anomalies) == 1
    assert "README.md" in report.anomalies[0]


def test_duplicate_across_batches_is_deduped_and_recorded(tmp_path):
    """同一号码出现在两批里：只采用一次，但把重复记下来。"""
    _tree(
        tmp_path,
        {"A/13415100087.json": "{}", "B/13415100087.json": "{}"},
    )
    report = ctp.scan(tmp_path)
    assert report.matched == ["13415100087"]
    assert len(report.duplicates) == 1


def test_read_write_roundtrip_ignores_comments(tmp_path):
    """清单读写：注释与空行被忽略，写入是 UTF-8 + LF + 结尾换行。"""
    path = tmp_path / "list.txt"
    ctp.write_phone_list(path, ["13415100087", "13430349943"])
    raw = path.read_bytes()
    assert raw == b"13415100087\n13430349943\n"
    assert b"\r" not in raw

    path.write_text("# 注释\n\n13415100087\n  \n13430349943\n", encoding="utf-8")
    assert ctp.read_phone_list(path) == ["13415100087", "13430349943"]


def test_write_is_utf8_without_bom(tmp_path):
    """落盘不带 BOM。带 BOM 的清单会被下游当成号码的一部分。"""
    path = tmp_path / "list.txt"
    ctp.write_phone_list(path, ["13415100087"])
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_missing_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        ctp.scan(tmp_path / "nope")


def test_scan_of_real_delivery_directory():
    """真实交付目录：39 个号码、21 个 macOS 残留、无异常。

    这条把「测试方给的批次长什么样」钉成事实；目录被换掉时它会失败，而不是让基线
    悄悄用上一份不同的号码集合。
    """
    if not REAL_ROOT.is_dir():
        pytest.skip(f"缺少 {REAL_ROOT}（测试数据由业务方提供，不入库）")
    report = ctp.scan(REAL_ROOT)
    assert len(report.matched) == 39
    assert report.anomalies == []
    assert report.duplicates == []
    assert len(report.junk) == 21
    assert report.matched == sorted(report.matched)


def test_committed_list_matches_directory():
    """入库的 ``test_phone_numbers.txt`` 与目录一致。

    它是基线的锚点——清单与目录漂移之后，「上次基线用的是哪批号」就说不清了。
    """
    if not REAL_ROOT.is_dir() or not REAL_LIST.is_file():
        pytest.skip("缺少交付目录或清单文件")
    listed = ctp.read_phone_list(REAL_LIST)
    report = ctp.scan(REAL_ROOT)
    assert listed == report.matched
    assert len(listed) == len(set(listed)) == 39


def test_check_mode_reports_drift(tmp_path):
    """``--check`` 在清单不一致时返回非 0，一致时返回 0。"""
    root = tmp_path / "data"
    _tree(root, {"A/13415100087.json": "{}", "B/13430349943.json": "{}"})
    out = tmp_path / "list.txt"

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(COLLECT_SCRIPT),
                "--root",
                str(root),
                "--out",
                str(out),
                "--check",
                *extra,
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    assert run().returncode == 1  # 清单还不存在

    ctp.write_phone_list(out, ["13415100087", "13430349943"])
    assert run().returncode == 0

    ctp.write_phone_list(out, ["13415100087"])
    drifted = run()
    assert drifted.returncode == 1
    assert "13430349943" in drifted.stdout


def test_check_mode_flags_anomalies(tmp_path):
    """目录里有异常时 ``--check`` 也返回非 0，即使号码集合恰好一致。"""
    root = tmp_path / "data"
    _tree(root, {"A/13415100087.json": "{}", "A/notes.json": "{}"})
    out = tmp_path / "list.txt"
    ctp.write_phone_list(out, ["13415100087"])
    result = subprocess.run(
        [
            sys.executable,
            str(COLLECT_SCRIPT),
            "--root",
            str(root),
            "--out",
            str(out),
            "--check",
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any("anomalies" in item for item in payload["problems"])


def test_json_mode_outputs_ascii_only(tmp_path):
    """``--json`` 的输出全 ASCII：控制台代码页不是 UTF-8 时也不会乱码。

    显式传 ``--out``：不带它时脚本会用默认路径，而默认路径是**仓库里那份真实清单**，
    会把受控文件覆盖成这个临时目录的内容。
    """
    root = tmp_path / "data"
    _tree(root, {"A/13415100087.json": "{}"})
    result = subprocess.run(
        [
            sys.executable,
            str(COLLECT_SCRIPT),
            "--root",
            str(root),
            "--out",
            str(tmp_path / "list.txt"),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0
    result.stdout.encode("ascii")  # 抛 UnicodeEncodeError 即为失败
    assert "13415100087" in result.stdout


def test_writing_never_touches_the_committed_list(tmp_path):
    """在临时目录上跑一次写清单，仓库里的受控清单必须原封不动。

    这条挡的是「测试自己有副作用」：默认 ``--out`` 指向仓库根的真实清单，忘了传
    ``--out`` 的用例会静默把它覆盖成测试数据里的号码集合。
    """
    if not REAL_LIST.is_file():
        pytest.skip("缺少受控清单")
    before = REAL_LIST.read_bytes()

    root = tmp_path / "data"
    _tree(root, {"A/13415100087.json": "{}"})
    subprocess.run(
        [
            sys.executable,
            str(COLLECT_SCRIPT),
            "--root",
            str(root),
            "--out",
            str(tmp_path / "list.txt"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert REAL_LIST.read_bytes() == before
