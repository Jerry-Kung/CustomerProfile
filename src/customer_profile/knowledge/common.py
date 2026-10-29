"""通用知识：跨合作方成立的那部分，与 ``packs/`` 同级。

**为什么邀约策略放这里而不是某个知识包里**：拍板第二条——这套七条策略留用、去品牌化
之后讲的是「怎么邀约」的方法，换合作方照样成立；而车型、价格、品牌话术是随合作方变的，
归 ``packs/<pack_id>/``。两者的生效期与更新节奏不同，因此不放在一个目录里。

**与 ``templates/`` 的关系**：第 0 期起这份正文一直在
``templates/customer_profile_shared__1776934470389.txt``，两处并存会让「改哪一份」变成
反复讨论的问题（见 ``docs/feature-knowledge-base/07`` R9）。第 2 期把它整体搬过来，
模板目录下那份随之删除——**只剩一处真源**。

三个口径：

1. **只读**。与知识包同一套约定：写入即冻结，改文件重启，没有写接口。
2. **装载失败退化，不抛**。取不到就返回空列表，表达层模板渲染出「无邀约策略」，
   卡片仍能生成——策略缺失只是少一层建议，不该让整张卡作废。
3. **装载期校验形状**。七条策略的必填字段缺失属编写错误，在预检里报出来；
   请求路径上仍走退化路径（同第 2 条）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

COMMON_DIR = Path(__file__).resolve().parent / "common"
"""通用知识目录：``knowledge/common/``。"""

INVITE_STRATEGY_FILE = "invite_strategy.json"

REQUIRED_KEYS: tuple[str, ...] = (
    "策略ID",
    "策略类别",
    "目标客户画像（标签）",
    "核心邀约钩子（The Hook）",
    "推荐邀约动作/场景",
    "话术示例",
    "AI触发标签（用于匹配）",
    "优先级",
)
"""一条策略的必填字段。缺任一字段即视为编写错误——

表达层拿到的是一段 JSON，字段名就是给模型看的小标题；少一个它不会报错，只会少写一段。
这类错误只能在装载期抓。
"""


class CommonError(ValueError):
    """通用知识装不起来。只在预检与测试路径上抛。"""


@dataclass(frozen=True, slots=True)
class InviteStrategy:
    """去品牌化后的通用邀约策略集合。"""

    items: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.items

    def as_text(self) -> str:
        """渲染成给模型的文本：缩进 JSON。**空集合返回空串。**

        用 JSON 而不是改写成散文：这份资产本来就是结构化条目，模型读 JSON 与读
        Markdown 表格的效果相当，而 JSON 保留了字段边界——日后按画像标签筛策略时
        不必再解析一遍自然语言。
        """
        if self.is_empty:
            return ""
        return json.dumps(list(self.items), ensure_ascii=False, indent=2)


EMPTY_INVITE = InviteStrategy()


def load_invite_strategy(path: Path | None = None) -> InviteStrategy:
    """装载并校验邀约策略。**形状不对即抛**——供预检与测试使用。"""
    target = path or (COMMON_DIR / INVITE_STRATEGY_FILE)
    if not target.is_file():
        raise CommonError(f"通用邀约策略不存在：{target}")

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CommonError(f"通用邀约策略不是合法 JSON：{target}：{exc}") from exc

    if not isinstance(data, list) or not data:
        raise CommonError(f"通用邀约策略应为非空数组，实际为 {type(data).__name__}")

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise CommonError(f"第 {index + 1} 条策略不是对象")
        missing = [key for key in REQUIRED_KEYS if not str(item.get(key) or "").strip()]
        if missing:
            raise CommonError(f"第 {index + 1} 条策略缺字段：{missing}")
        sid = str(item["策略ID"]).strip()
        if sid in seen:
            raise CommonError(f"策略编号重复：{sid}")
        seen.add(sid)
        items.append(dict(item))

    return InviteStrategy(items=tuple(items))


def current_invite_strategy(path: Path | None = None) -> InviteStrategy:
    """当前生效的邀约策略。**永不抛**——装载失败返回空集合并记一次日志。

    与 :func:`customer_profile.knowledge.current_pack` 同一取向：硬失败留给预检，
    请求路径上只退化。这里不缓存——文件只有一份、只在 L3 每案取一次，
    缓存带来的收益不足以抵掉「测试注入临时目录时串味」的风险。
    """
    try:
        return load_invite_strategy(path)
    except Exception as exc:  # noqa: BLE001 —— 任何装载失败都退化为空
        logger.warning("通用邀约策略装载失败，本次不注入邀约策略：{}", exc)
        return EMPTY_INVITE
