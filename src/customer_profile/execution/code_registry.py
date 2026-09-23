"""``code`` 节点的函数登记表。

原 Dify ``code`` 节点里的 Python 直接提取为普通函数，登记在这里，节点定义只保存一个
``config['function']`` 引用。这样业务逻辑是「人写的普通代码」，而不是节点图的一部分
（规划 §6.1）。

登记用 ``module:callable`` 形式，例如
``customer_profile.workflows.mengshi_it_system_data:extract_channel_payload``。
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Mapping

_REGISTRY: dict[str, Callable[..., Any]] = {}


class CodeFunctionNotFound(KeyError):
    """引用的 code 函数未登记或无法导入。"""


def register_code_function(
    name: str, function: Callable[..., Any] | None = None
) -> Any:
    """登记一个 code 函数。可作装饰器：``@register_code_function("name")``。"""
    if function is None:

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            _REGISTRY[name] = func
            return func

        return decorator
    _REGISTRY[name] = function
    return function


def get_code_function(
    ref: str, *, extra: Mapping[str, Callable[..., Any]] | None = None
) -> Callable[..., Any]:
    """按引用取函数。

    查找顺序：本次运行注入的 ``extra`` → 本进程登记表 → ``module:callable`` 动态导入。
    找不到时抛 :class:`CodeFunctionNotFound`，并列出当前登记表，避免只报「找不到」。
    """
    if extra and ref in extra:
        return extra[ref]
    if ref in _REGISTRY:
        return _REGISTRY[ref]

    if ":" in ref:
        module_name, _, attr = ref.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise CodeFunctionNotFound(
                f"无法导入模块 {module_name!r}：{exc}"
            ) from exc
        try:
            function = getattr(module, attr)
        except AttributeError as exc:
            raise CodeFunctionNotFound(
                f"模块 {module_name!r} 中没有 {attr!r}"
            ) from exc
        _REGISTRY[ref] = function
        return function

    raise CodeFunctionNotFound(
        f"code 函数 {ref!r} 未登记。已登记：{sorted(_REGISTRY)}；"
        "或改用 module:callable 形式引用"
    )


def registered_names() -> list[str]:
    """当前进程内已登记的函数名，供体检与测试使用。"""
    return sorted(_REGISTRY)


def clear_registry() -> None:
    """清空登记表（测试用）。"""
    _REGISTRY.clear()


def autodiscover() -> list[str]:
    """导入全部工作流模块，让其中的登记副作用生效。

    返回成功导入的模块名列表。某个模块导入失败时不静默跳过——直接抛出，
    因为「悄悄少了一个工作流」比构建失败更难查。
    """
    from .. import workflows as workflows_package

    imported: list[str] = []
    for module_name in workflows_package.WORKFLOW_MODULES:
        importlib.import_module(module_name)
        imported.append(module_name)
    return imported
