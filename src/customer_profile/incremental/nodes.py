"""增量入口工作流的 code 节点实现。

这些函数由 ``runner.build_service`` 以 ``functools.partial`` 注入运行期依赖（Store），
因为 ``code`` 节点的契约只有 ``**inputs``，拿不到 :class:`NodeRuntime`——与 checker /
枚举映射同一套注入方式，注入点都是 ``rt.extra['code_functions']``。

**为什么基线在这里写、而不是在收尾钩子里**（对 ``03`` T1-2 的一处实现选择，口径不变）：
写基线需要两样东西——本次发出的回传体与本轮的 history 原始响应。收尾钩子手里只有前者；
后者要么从 ``request_attempts`` 反查（存储瘦身后载荷可能已被搬进包），要么再抓一次接口。
本工作流在 ``dispatch`` 节点手里正好两者都有，写在这里是**同一个事实来源**，也少一处
可能静默对不上的重建逻辑。代价是「增量入口关闭时不会预建基线」；P3 灰度时若要提前
铺基线，用脚本跑一批全量即可（见 ``03`` P3）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..persistence.profile_baselines import ProfileBaseline, merge_keys
from . import dispatch as dp
from .compat import compat_key
from .diff import consumed_keys as keys_of_rows


def _rows(history_body: Any) -> list[dict]:
    return dp._rows(history_body)


async def incremental_dispatch(
    history_body: Any = None,
    profile_body: Any = None,
    phone_number: str = "",
    batch_id: str = "",
    *,
    store: Any = None,
) -> dict[str, Any]:
    """读基线 → 定档 → 产出 ``dispatch`` 节点输出（含 T0 的回传体副本）。"""
    baseline = None
    error = ""
    if store is not None:
        from ..persistence.profile_baselines import BaselineRepository

        try:
            baseline = await BaselineRepository(store).load(str(phone_number))
        except Exception as exc:  # noqa: BLE001 —— 读基线失败不是业务失败，退回全量
            error = f"{type(exc).__name__}: {exc}"

    try:
        current_key = compat_key()
    except Exception as exc:  # noqa: BLE001
        current_key = ""
        error = (error + " | " if error else "") + f"兼容键计算失败：{type(exc).__name__}: {exc}"

    result = dp.decide(
        history_body=history_body,
        phone=str(phone_number),
        batch_id=str(batch_id),
        baseline=baseline,
        current_compat_key=current_key,
        profile_body=profile_body,
        enabled_tiers=_enabled_tiers(),
    )
    result["compat_key"] = current_key
    result["adapt_error"] = error
    result["history_records"] = len(_rows(history_body))
    return result


async def baseline_commit(
    phone_number: str = "",
    batch_id: str = "",
    callback_json: Any = "",
    history_body: Any = None,
    tier: str = "",
    *,
    store: Any = None,
) -> dict[str, Any]:
    """把本次发出的回传体与已消费键写进基线。

    **失败不阻断**：基线是加速与稳定性的手段，写不进去最多是下一次落全量，不该让
    一次已经算完、已经回写的运行因此失败。
    """
    if store is None:
        return {"baseline_saved": "false", "baseline_error": "没有可用存储"}

    from ..persistence.profile_baselines import BaselineRepository

    try:
        payload = json.loads(callback_json) if isinstance(callback_json, str) else callback_json
    except ValueError:
        return {"baseline_saved": "false", "baseline_error": "回传体不是合法 JSON"}
    if not isinstance(payload, dict) or not isinstance(payload.get("analysis_result"), dict):
        return {"baseline_saved": "false", "baseline_error": "回传体结构异常（缺 analysis_result）"}

    rows = _rows(history_body)
    level, level_detail = dp.level_of(payload)
    baseline = ProfileBaseline(
        phone=str(phone_number),
        compat_key=compat_key(),
        source_run_id="",
        updated_at_ms=int(time.time() * 1000),
        stage=dp.stage_of(rows),
        deal_level=level,
        deal_level_detail=level_detail,
        consumed_keys=tuple(keys_of_rows(rows)),
        callback=payload,
    )
    # 同一手机号重复运行时键集合只增不减：上一轮已消费但本轮接口没返回的记录，
    # 若从基线里删掉，下一轮会把它们重新当成「新增」，T0 就永远命中不了。
    try:
        previous = await BaselineRepository(store).load(str(phone_number))
        if previous is not None:
            baseline.consumed_keys = merge_keys(previous.consumed_keys, baseline.consumed_keys)
    except Exception:  # noqa: BLE001 —— 读不到旧基线不影响写入
        pass

    try:
        await BaselineRepository(store).save(baseline)
    except Exception as exc:  # noqa: BLE001
        return {"baseline_saved": "false", "baseline_error": f"{type(exc).__name__}: {exc}"}
    return {
        "baseline_saved": "true",
        "baseline_error": "",
        "baseline_keys": str(len(baseline.consumed_keys)),
        "baseline_tier": str(tier or ""),
    }


def _enabled_tiers() -> tuple[str, ...]:
    """放行哪些档位。P2 起 T1 / T2 放开（``03`` T2-7）。

    读配置而不是写死：``INCREMENTAL_TIERS`` 置空即回到「一律落 T3」，那是灰度期
    最省事的回滚方式，不需要改代码。
    """
    try:
        from ..settings import get_settings

        raw = str(getattr(get_settings(), "incremental_tiers", "") or "").strip()
    except Exception:  # noqa: BLE001 —— 读不到配置按放开处理
        return dp.DEFAULT_TIERS
    if not raw:
        return dp.DEFAULT_TIERS
    picked = tuple(item.strip().upper() for item in raw.split(",") if item.strip())
    return picked or dp.DEFAULT_TIERS


def reuse_callback(value: Any = "", batch_id: str = "") -> dict[str, Any]:
    """T0：副本原样回传，只换 ``batch_id``。

    ``dispatch`` 已经把副本换成新批次号了；这里再兜一次是因为批次号属于**任务侧**
    契约（回传体要带回原值，前端据此判断任务来源），不该依赖上游是否填对。
    """
    try:
        payload = json.loads(value) if isinstance(value, str) else dict(value or {})
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["batch_id"] = str(batch_id or payload.get("batch_id") or "")
    return {"callback_json": json.dumps(payload, ensure_ascii=False)}


def passthrough_t0(value: Any = "") -> dict[str, Any]:
    """T0 分支的输出口。``tier_used`` 让运行台不必回看 dispatch 也能看出走了哪支。"""
    return {"callback_json": value, "tier_used": dp.TIER_T0}


def passthrough_t3(value: Any = "") -> dict[str, Any]:
    """T3 分支的输出口。``result1`` 是现行管线的 ``end`` 产物（回传体 JSON 文本）。"""
    return {"callback_json": value, "tier_used": dp.TIER_T3}


def passthrough(value: Any = "") -> dict[str, Any]:
    """把一路分支的产物原样交出去，供汇合点取用。

    ``variable-aggregator`` 按「来源节点是否执行过」取值而不是按「值是否非空」——
    这正好适配本用例：两条分支只有一条会执行，另一条整个子树是 SKIPPED。
    """
    return {"callback_json": value}
