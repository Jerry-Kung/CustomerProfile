"""定级提示词与业务规范的一致性（T2-4，处置 ``07`` P2）。

## 为什么是「同步校验」而不是「只引用不抄写」

原计划让提示词只引用 `docs/reference/客户商机等级定级规范.md`。这条路走不通：
镜像只 COPY `src/`、`frontend/dist/` 与回放夹具（见 `Dockerfile` 与 `.dockerignore`），
`docs/` 不进镜像。提示词若「引用」一份运行期读不到的文件，模型拿到的就是一句空话。

因此采取第二条路：**正文留在提示词里，但用一条测试把它钉在规范原文上**。
两份副本同时存在，漂移在 CI 里立刻现形——这比「靠人记得同步」强，也比
「把 470 行规范塞进提示词」省 token。

规范文档是**业务方原文**，改动须经业务确认；提示词是执行口径，需要精简与
面向模型的措辞。两者不应逐字相同，能对齐的是**等级骨架**：每个等级的含义与
时间窗口。这正是本测试比对的那部分。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DOC = REPO_ROOT / "docs" / "reference" / "客户商机等级定级规范.md"
PROMPT = (
    REPO_ROOT
    / "src"
    / "customer_profile"
    / "templates"
    / "customer_profile_pipeline__pipeline_level_assess.txt"
)

LEVELS = ("H", "A", "B", "C", "待识别")
"""等级全集。``D`` 不在此列——它只为历史批次兼容而存在，不是可评出的等级。"""

WINDOWS = {
    "H": "7天",
    "A": "14天",
    "B": "30天",
    "C": "30天",
}
"""每个等级的时间窗口，写成**去空白**形态。

比对基准是 ``_squash`` 之后的正文，而 ``_squash`` 已经把空格去掉了——
这里若写「7 天」，拼出的 needle 会带着那个空格，永远匹配不上。
"""


def _squash(text: str) -> str:
    """去掉空白与加粗标记，只留用于比对的内容。"""
    return re.sub(r"[\s*`]", "", text)


@pytest.fixture(scope="module")
def prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def spec_text() -> str:
    return SPEC_DOC.read_text(encoding="utf-8")


def test_spec_doc_exists_and_has_the_level_table(spec_text):
    """规范原文在仓库里且含等级表——否则本用例失去比对基准。"""
    assert SPEC_DOC.is_file(), f"规范的唯一真源不存在：{SPEC_DOC}"
    squashed = _squash(spec_text)
    for level in LEVELS:
        assert f"**{level}级**" in spec_text or f"**{level}**" in spec_text, (
            f"规范里找不到等级 {level} 的定义行"
        )
        assert level in squashed


def test_prompt_table_covers_every_level(prompt_text):
    """提示词的等级表必须覆盖全部五个等级，一个不漏。

    ``_squash`` 会去掉空白与 ``*``，因此 ``| **H** |`` 在这里是 ``|H|``。
    """
    squashed = _squash(prompt_text)
    for level in LEVELS:
        assert f"|{level}|" in squashed, f"提示词等级表缺 {level}"


@pytest.mark.parametrize("level,window", sorted(WINDOWS.items()))
def test_time_window_matches_spec(prompt_text, spec_text, level, window):
    """每个等级的时间窗口必须与规范一致。

    这是两份副本最易漂移的一处：业务方调整窗口（比如 H 从 7 天改成 5 天），
    提示词不改就会让模型按旧窗口定级，而**没有任何报错**——只有这条用例能拦住。
    """
    prompt_flat = _squash(prompt_text)
    spec_flat = _squash(spec_text)
    # 两份表的列名不同（提示词「等级」、规范「AI等级」），且规范的等级值带「级」字，
    # 因此只在**行首锚点**上求同：`|<等级>|预计<N>天内`。
    assert f"|{level}|预计{window}内" in prompt_flat, (
        f"提示词里 {level} 级的时间窗口不是「{window}内」"
    )
    assert f"|{level}级|预计{window}内" in spec_flat, (
        f"规范里 {level} 级的时间窗口不是「{window}内」——若业务方调过窗口，提示词需同步"
    )


def test_lead_stage_cap_is_stated_in_both(prompt_text, spec_text):
    """线索阶段上限为 A 这条硬约束，两份都要有。

    `LEVEL-02` 在代码里强制收口，但提示词若不说，模型会持续给出 H 并被反复降级——
    产物对但浪费 token，且推理链与最终等级不自洽。
    """
    for text, name in ((prompt_text, "提示词"), (spec_text, "规范")):
        squashed = _squash(text)
        assert "线索邀约阶段" in squashed
        assert "最高" in squashed and "A" in squashed, f"{name}里没写线索阶段的 A 级上限"


def test_three_evidence_types_are_named_in_both(prompt_text, spec_text):
    """H/A 的三类证据（需求 / 时间 / 行动）两份都要点名。"""
    for text, name in ((prompt_text, "提示词"), (spec_text, "规范")):
        squashed = _squash(text)
        for kind in ("需求证据", "时间证据", "行动证据"):
            assert kind in squashed, f"{name}缺「{kind}」"
