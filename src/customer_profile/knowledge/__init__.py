"""知识库包：把「我们在卖什么」变成代码能查、模型能读、换伙伴能换的一份资产。

四个能力（见 ``docs/feature-knowledge-base/01-功能特性设计.md``）：

- :mod:`.pack` —— 加载与校验（K1）
- :mod:`.resolve` —— 品牌与车型解析（K2）
- :mod:`.views` —— 取证侧与表达侧视图（K3、K4）
- :mod:`.intent` —— 意向车型闭集与四路分流（K5）

入口只有三个：:func:`current_pack`、:func:`load_pack`、:func:`reset_cache`。
**没有写接口**——知识内容写入即冻结，改文件重启，与运维约定一致。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from ..settings import get_settings
from .pack import (
    DEFAULT_PACK_DIR,
    EMPTY_PACK,
    BrandFile,
    BrandIdentity,
    ConfigEntry,
    Manifest,
    ModelEntry,
    Pack,
    PackError,
    Price,
    Tier,
    load_pack,
    normalize_name,
)

__all__ = [
    "DEFAULT_PACK_DIR",
    "EMPTY_PACK",
    "Pack",
    "PackError",
    "current_pack",
    "load_pack",
    "normalize_name",
    "intent",
    "packs_root",
    "report",
    "reset_cache",
    "resolve",
    "views",
]

_CACHE: dict[str, Pack] = {}
"""进程内缓存。键是**包目录的绝对路径**，不是包 ID——测试注入临时目录时两个包可能
共用同一个 ``pack_id``，按 ID 缓存会让第二个测试读到第一个的包。"""


def packs_root(settings=None) -> Path:
    """知识包根目录。``KNOWLEDGE_PACKS_DIR`` 非空时覆盖包内缺省目录（供测试注入）。"""
    resolved = settings or get_settings()
    raw = str(getattr(resolved, "knowledge_packs_dir", "") or "").strip()
    return Path(raw) if raw else DEFAULT_PACK_DIR


def _configured_pack_id(settings=None) -> str:
    resolved = settings or get_settings()
    return str(getattr(resolved, "knowledge_pack", "") or "").strip()


def current_pack(settings=None) -> Pack:
    """当前生效的知识包。**永不抛异常**——取不到就返回 :data:`EMPTY_PACK`。

    容器里 ``get_settings()`` 读的是同一份环境配置，因此「换伙伴只改
    ``KNOWLEDGE_PACK``」这句话在调用点无需再传参。

    失败不抛的理由见 ``docs/feature-knowledge-base/07`` R1：加载期校验是严格的，
    但它发生在预检与启动期；请求路径上知识不可用只应让效果退化，不应让管线停摆。
    空包的消费效果是「视图输出空串、分流跳过」，与接入知识前逐字节一致。
    """
    name = _configured_pack_id(settings)
    if not name:
        return EMPTY_PACK

    directory = packs_root(settings) / name
    key = str(directory.resolve())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        pack = load_pack(directory)
    except Exception as exc:  # noqa: BLE001 —— 任何装载失败都退化为空包
        # 只在这里记一次：调用点每案都会取包，逐次记日志会把日志刷满。
        logger.warning(
            "知识包 {!r} 装载失败，本次运行退化为不启用知识：{}", name, exc
        )
        pack = EMPTY_PACK

    _CACHE[key] = pack
    return pack


def reset_cache() -> None:
    """清空缓存。供测试与「改完知识文件不想重启」的本地调试使用。

    生产语义仍是「改文件重启」——这里不对外暴露热更新路径。
    """
    _CACHE.clear()


# 子模块在**文件末尾**导入：``report`` 依赖上面定义的 ``current_pack``，
# 置顶会形成包内循环导入。放在这里之后，调用点可直接写 ``knowledge.views.xxx``。
from . import intent, report, resolve, views  # noqa: E402,F401
