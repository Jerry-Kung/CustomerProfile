"""品牌名残留守卫（T3-1）。

## 为什么要有这条守卫

第 0–2 期把埃安资产摘掉、把提示词里的品牌举例中性化。这些改动**不会自我维持**：
复制一段旧提示词、从别处粘一个示例进模板，品牌名就回来了，而产物只是「慢慢偏掉」，
不会有任何报错。守卫把这件事从「靠人记得」变成「跑一次就知道」。

三组用例，分别钉住三件事：

1. **能发现**——造一个含品牌名的文件，必须红；
2. **不该误报**——知识包目录、豁免条目覆盖的位置必须放行；
3. **豁免会过期**——品牌名删了但豁免留着，也必须报出来，否则守卫悄悄变松。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.knowledge import brand_guard as bg


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个只含待扫描文件的临时根。"""
    (tmp_path / "sample.txt").write_text("这一行没有品牌名。\n", encoding="utf-8")
    return tmp_path


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 能发现

def test_forbidden_name_is_found(workspace):
    """含品牌名的行必须被报出来。"""
    _write(workspace, "hit.txt", "客户说他看过坦克 700。\n")
    hits = bg.scan(workspace, exemptions=())
    assert [h.brand for h in hits] == ["坦克"]
    assert hits[0].line == 1


def test_our_brand_from_pack_is_found(workspace):
    """**本方品牌从知识包现读**——换合作伙伴后守卫自动跟着换，不改代码。"""
    _write(workspace, "hit.txt", "话术里写了东风猛士。\n")
    hits = bg.scan(workspace, exemptions=())
    assert any(h.brand in ("东风猛士", "猛士") for h in hits)


def test_clean_file_produces_no_hit(workspace):
    """反例：没有品牌名就不该报——守卫不该成为噪声源。"""
    assert bg.scan(workspace, exemptions=()) == []


def test_longest_name_wins_without_duplicates(workspace):
    """同一处只记一条命中。

    「东风猛士」的四个称谓都在词表里，若逐名匹配会让一句话产生四条命中，
    报告被噪声淹没、豁免清单也要为同一句话写四遍。
    """
    _write(workspace, "hit.txt", "东风猛士在售。\n")
    hits = bg.scan(workspace, exemptions=())
    assert len(hits) == 1
    assert hits[0].brand == "东风猛士"


# ---------------------------------------------------------------- 不该误报

def test_knowledge_pack_directory_is_skipped(workspace):
    """知识包目录整目录放行：那里就是知识本身。"""
    _write(
        workspace,
        "knowledge/packs/x/models.json",
        '{"canonical_name": "猛士917"}\n',
    )
    assert bg.scan(workspace, exemptions=()) == []


def test_exemption_by_exact_path_covers_only_that_file(workspace):
    """豁免按「路径 + 品牌」成对生效，不连带放行同目录的其它文件。"""
    _write(workspace, "a.txt", "记得提一句坦克。\n")
    _write(workspace, "b.txt", "这里也提了坦克。\n")
    exemption = bg.Exemption(path="a.txt", brand="坦克", reason="测试用")
    hits = bg.scan(workspace, exemptions=(exemption,))
    assert [Path(h.path).name for h in hits] == ["b.txt"]


def test_exemption_does_not_leak_across_brands(workspace):
    """豁免一个品牌，不该顺带放行同一文件里的另一个品牌。"""
    _write(workspace, "a.txt", "坦克 与 别克 都提到了。\n")
    exemption = bg.Exemption(path="a.txt", brand="坦克", reason="测试用")
    hits = bg.scan(workspace, exemptions=(exemption,))
    assert [h.brand for h in hits] == ["别克"]


def test_directory_exemption_needs_trailing_slash():
    """目录豁免必须显式写结尾的 /，否则视为单文件——这是刻意的：误写成目录会放宽太多。"""
    root = bg.Exemption(path="sub", brand="坦克", reason="测试")
    assert not root.covers("sub/a.txt", "坦克")
    directory = bg.Exemption(path="sub/", brand="坦克", reason="测试")
    assert directory.covers("sub/a.txt", "坦克")


# ---------------------------------------------------------------- 豁免会过期

def test_stale_exemption_is_reported(workspace):
    """品牌名已删、豁免还留着 → 报出来。否则同位置日后再写回来就没人拦。"""
    _write(workspace, "a.txt", "这一行是干净的。\n")
    exemption = bg.Exemption(path="a.txt", brand="坦克", reason="早已清理")
    assert bg.uncovered_exemptions(workspace, exemptions=(exemption,)) == [exemption]


def test_live_exemption_is_not_stale(workspace):
    """仍在命中的豁免不算过期——否则守卫会一直红。"""
    _write(workspace, "a.txt", "这里还有坦克。\n")
    exemption = bg.Exemption(path="a.txt", brand="坦克", reason="合法用途")
    assert bg.uncovered_exemptions(workspace, exemptions=(exemption,)) == []


# ---------------------------------------------------------------- 豁免清单文件

def test_shipped_exemption_file_parses_and_every_entry_has_a_reason():
    """仓库里那份清单必须可解析，且每条都写了理由。

    「没有理由的豁免」等价于删除检查——日后没人知道它为什么在那里。
    """
    data = json.loads(bg.EXEMPTIONS_FILE.read_text(encoding="utf-8"))
    items = data["exemptions"] if isinstance(data, dict) else data
    assert items, "清单不该为空——空清单意味着守卫会对现有合法用途误报"
    for item in items:
        assert item.get("path") and item.get("brand")
        assert len(str(item.get("reason") or "").strip()) >= 8, f"理由过短：{item}"


def test_exemption_entries_without_reason_are_dropped(tmp_path):
    """缺理由的条目被忽略——宁可让守卫变严，也不接受无据的放行。"""
    path = tmp_path / "ex.json"
    path.write_text(
        json.dumps({"exemptions": [{"path": "a.txt", "brand": "坦克", "reason": ""}]}),
        encoding="utf-8",
    )
    assert bg.load_exemptions(path) == ()


def test_missing_exemption_file_means_no_exemptions(tmp_path):
    """文件缺失视为空清单：守卫更严，不会因此崩掉。"""
    assert bg.load_exemptions(tmp_path / "nope.json") == ()


# ---------------------------------------------------------------- 仓库现状

def test_repository_is_clean():
    """**仓库当前无未豁免命中、无失效豁免。**

    这条红了通常只有两种原因：提示词/代码里新写了品牌名（该改），
    或者删了品牌名但豁免没跟着删（该删条目）。
    """
    hits = bg.scan()
    stale = bg.uncovered_exemptions()
    assert not hits, "未豁免的品牌名：\n" + "\n".join(str(h) for h in hits)
    assert not stale, "失效的豁免：\n" + "\n".join(f"{s.path} [{s.brand}]" for s in stale)


def test_report_shape():
    report = bg.report([], [])
    assert report["ok"] is True
    assert set(report) == {"ok", "hits", "stale_exemptions", "brand_count"}
    assert report["brand_count"] > 0
