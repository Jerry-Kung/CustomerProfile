"""跨工作流复用的 ``code`` 节点逻辑。

DSL 里同一段 Python 被复制进多个工作流（例如「检查 XX 文件」在 8 个工作流里逐字重复、
「数组格式转换」在 6 个里重复）。迁移后它应当是**一个函数**，因此集中在这里，由各
工作流模块按名引用。

写法遵循规划 §6.2：函数体与 DSL 的 ``main()`` **逐字符一致**，只把 ``def main(...)``
换成业务化的名字、并按需补中文 docstring（`tests/test_code_verbatim.py` 对这两项放行，
可执行语句一律不得改动）。
"""

from __future__ import annotations

import json
from typing import Any


# ====================================================================
# 渠道载荷提取：8 个工作流共用（7 个截图类 + 2 个录音类中的同形节点）
# ====================================================================


def extract_media_urls(records: str, data_source: str) -> dict:
    """从 ``customer_data`` 里取出 ``channel == data_source`` 那一项的 ``media_urls``。

    与 DSL 节点（支付宝/抖音/朋友圈/微信主页/微信搜索/小红书 的 ``1775702981484``，
    以及试驾/外呼录音的 ``1775702981484``）的 ``main`` 函数体逐字符一致。
    """
    import json

    # 兜底：输入为空时直接返回空字符串
    if records is None:
        return {"result": ""}

    records = records.strip()
    if not records:
        return {"result": ""}

    # 解析 JSON 字符串
    try:
        data = json.loads(records)
    except Exception:
        return {"result": ""}

    # 顶层必须是数组
    if not isinstance(data, list):
        return {"result": ""}

    # 遍历数组元素，查找 channel == data_source
    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("channel") == data_source:
            media_urls = item.get("media_urls")

            # 若找到目标元素，但 media_urls 为空/null，则返回空字符串
            if media_urls is None:
                return {"result": ""}

            # 若 media_urls 本身就是字符串，直接返回
            if isinstance(media_urls, str):
                return {"result": media_urls}

            # 若 media_urls 是数组/对象，转成 JSON 字符串返回
            return {"result": json.dumps(media_urls, ensure_ascii=False)}

    # 没找到目标元素
    return {"result": ""}


def extract_raw_payload(records: str, data_source: str) -> dict:
    """同上，但取 ``raw_payload``（聊天记录、猛士IT、Feedback 等用这个字段）。

    与 DSL 节点 ``1776328382353``（聊天记录/猛士IT/Feedback）的 ``main`` 逐字符一致。
    """
    import json

    # 兜底：输入为空时直接返回空字符串
    if records is None:
        return {"result": ""}

    records = records.strip()
    if not records:
        return {"result": ""}

    # 解析 JSON 字符串
    try:
        data = json.loads(records)
    except Exception:
        return {"result": ""}

    # 顶层必须是数组
    if not isinstance(data, list):
        return {"result": ""}

    # 遍历数组元素，查找 channel == data_source
    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("channel") == data_source:
            raw_payload = item.get("raw_payload")

            # 若找到目标元素，但 media_urls 为空/null，则返回空字符串
            if raw_payload is None:
                return {"result": ""}

            # 若 media_urls 本身就是字符串，直接返回
            if isinstance(raw_payload, str):
                return {"result": raw_payload}

            # 若 media_urls 是数组/对象，转成 JSON 字符串返回
            return {"result": json.dumps(raw_payload, ensure_ascii=False)}

    # 没找到目标元素
    return {"result": ""}


def extract_related_enterprise(records: str) -> dict:
    """提取关联企业信息（channel 固定为 ``RelatedEnterpriseInfo``）。

    与 DSL 节点 ``1789984908249`` 的 ``main`` 逐字符一致。
    """
    import json
    data_source = "RelatedEnterpriseInfo"

    # 兜底：输入为空时直接返回空字符串
    if records is None:
        return {"result": ""}

    records = records.strip()
    if not records:
        return {"result": ""}

    # 解析 JSON 字符串
    try:
        data = json.loads(records)
    except Exception:
        return {"result": ""}

    # 顶层必须是数组
    if not isinstance(data, list):
        return {"result": ""}

    # 遍历数组元素，查找 channel == data_source
    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("channel") == data_source:
            raw_payload = item.get("raw_payload")

            # 若找到目标元素，但 media_urls 为空/null，则返回空字符串
            if raw_payload is None:
                return {"result": ""}

            # 若 media_urls 本身就是字符串，直接返回
            if isinstance(raw_payload, str):
                return {"result": raw_payload}

            # 若 media_urls 是数组/对象，转成 JSON 字符串返回
            return {"result": json.dumps(raw_payload, ensure_ascii=False)}

    # 没找到目标元素
    return {"result": ""}


def extract_jiguang_tags(records: str, data_source: str) -> dict:
    """提取极光数据的 ``tags``。

    与 DSL 节点 ``1777288063102`` 的 ``main`` 逐字符一致。
    """
    import json

    # 兜底：输入为空时直接返回空字符串
    if records is None:
        return {"result": ""}

    records = records.strip()
    if not records:
        return {"result": ""}

    # 解析 JSON 字符串
    try:
        data = json.loads(records)
    except Exception:
        return {"result": ""}

    # 顶层必须是数组
    if not isinstance(data, list):
        return {"result": ""}

    # 遍历数组元素，查找 channel == data_source
    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("channel") == data_source:
            media_data = item.get("raw_payload")
            if not media_data:
                return {"result": ""}

            user_tags = media_data.get("tags")

            # 若找到目标元素，但 user_tags 为空/null，则返回空字符串
            if user_tags is None:
                return {"result": ""}

            # 若 user_tags 本身就是字符串，直接返回
            if isinstance(user_tags, str):
                return {"result": user_tags}

            # 若 user_tags 是数组/对象，转成 JSON 字符串返回
            return {"result": json.dumps(user_tags, ensure_ascii=False)}

    # 没找到目标元素
    return {"result": ""}


# ====================================================================
# 数组转换：6 个截图工作流共用（键名 images）
# ====================================================================


def parse_media_url_array(images: list) -> dict:
    """把上游的 JSON 字符串解析成数组，并给出元素个数。

    与 DSL 节点 ``1775705336479``（截图类）的 ``main`` 逐字符一致。
    """
    if images is None:
        images = []
    return {
        "images": json.loads(images),
        "count": len(json.loads(images))
    }


def parse_file_array(files: list) -> dict:
    """同上的录音类版本（键名 ``files``）。

    与 DSL 节点 ``1775705336479``（录音类）的 ``main`` 逐字符一致。
    """
    if files is None:
        files = []
    return {
        "files": json.loads(files),
        "count": len(json.loads(files))
    }


def first_image_url(images: list[str]) -> dict:
    """取第一张图片的 URL。与 DSL 节点 ``1775717470046``（截图类）逐字符一致。"""
    return {
        "image_url": images[0],
    }


def first_file_url(files: list[str]) -> dict:
    """取第一个文件的 URL。与 DSL 节点 ``1775717470046``（录音类）逐字符一致。"""
    return {
        "file_url": files[0],
    }


# ====================================================================
# 人设特征：分组聚合结果拆解（1 处）
# ====================================================================


def split_grouped_features(
    hobby_result_object, consumption_result_object
) -> dict:
    """把分组聚合器的输出对象拆成两个独立结果。

    与 DSL 节点 ``1776760868761`` 的 ``main`` 逐字符一致。
    """
    return {
        "hobby_result": hobby_result_object["output"],
        "consumption_result": consumption_result_object["output"]
    }


# ====================================================================
# 常用的小工具（供上面各函数所在的模块复用）
# ====================================================================


def parse_json_object(value: Any) -> dict:
    """兼容 String / Object / None 的 JSON 对象解析。

    这是若干 DSL 节点里各自重复出现的 ``_parse_json`` 的**共同形态**：解析失败、
    参数为空、非对象类型一律返回空 dict。各处原版的差异只在异常处理宽严，行为一致。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹。各 DSL 节点的 ``_strip_code_fence`` 行为一致。"""
    import re

    text = text.strip()
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ====================================================================
# AUC 语音识别链路（audio_content_extract 专用）
# ====================================================================


def extract_auc_result(body: str) -> dict:
    """从 ``/query_analyze`` 的响应里取出识别结果与轮询计数。

    与 DSL 节点 ``1773125494862`` 的 ``main`` 函数体逐字符一致。
    """
    try:
        # 第一步：解析外层body的JSON字符串
        body_json = json.loads(body)
        # 第二步：提取auc_result（此时仍是字符串化的数组）
        auc_result_str = body_json.get("auc_result", "[]")
        poll_count = body_json.get("poll_count", 0)  # 修正：统一4个空格缩进，移除Tab
        return {"auc_result": auc_result_str, "poll_count": poll_count}
    except Exception as e:
        # 修正：except中poll_count可能未定义，给默认值0
        return {"auc_result": f"解析失败: {str(e)}", "poll_count": 0}


def extract_validity(result: str) -> dict:
    """从有效性判断的输出里取出 ``is_valid``。

    与 DSL 节点 ``17732159935030`` 的 ``main`` 函数体逐字符一致。
    下游 ``if-else`` 以它为条件，因此这里解析失败按「无效」处理是刻意的。
    """
    try:
        # 第一步：解析外层body的JSON字符串
        body_json = json.loads(result)
        # 第二步：提取auc_result（此时仍是字符串化的数组）
        is_valid = body_json.get("is_valid", True)
        return {"is_valid": is_valid}
    except Exception as e:
        # 修正：except中poll_count可能未定义，给默认值0
        return {"is_valid": False}


def parse_task_params(body: str) -> dict:
    """从 ``/submit_analyze`` 的响应里取出 ``task_id`` 与 ``x_tt_logid``。

    与 DSL 节点 ``17731967052740``（获取任务参数）的 ``main`` 函数体逐字符一致。
    解析失败时把错误信息本身当作返回值，是原实现的刻意设计：让下游请求带上
    ``解析失败: ...`` 而不是空串，错误因此出现在服务端日志里，比静默的空值好排查。
    """
    try:
        # 第一步：解析外层body的JSON字符串
        body_json = json.loads(body)
        # 第二步：提取auc_result（此时仍是字符串化的数组）
        task_id = body_json.get("task_id", "")
        x_tt_logid = body_json.get("x_tt_logid", "")

        return {"task_id": task_id, "x_tt_logid": x_tt_logid}
    except Exception as e:
        return {"task_id": f"解析失败: {str(e)}", "x_tt_logid": f"解析失败: {str(e)}"}
