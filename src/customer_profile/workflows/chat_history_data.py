"""``（新）SubAgent - 聊天记录数据信息`` 的 Python 定义。

DSL 基线：9 节点 / 9 边。链路是「取聊天记录 → 清洗两遍 → 聚合」：

    start(phone_number, customer_data, data_source)
      └─ code 数据提取（按 channel 取 raw_payload）
           └─ if-else 条件分支
                ├─ true  → code 数据初步清洗&格式转换
                │            └─ template 数据深度清洗Prompt
                │                 └─ llm 数据深度清洗（原 gemini_retry 工具节点）
                └─ else  → template 无数据，输出默认信息
                              └─ variable-aggregator → end

两处口径：

- ``llm`` 节点由 DSL 的 ``tool: gemini_retry_2_times`` 归一而来（有意差异 W2/W3）。
  Dify 把它做成一个**子流程**，内含 3 次 LLM 执行 + 2 次异常判断。迁移后重试下沉到
  ``llm_call()`` 内部，因此这里只是一个普通 ``llm`` 节点。输出字段仍叫 ``result``，
  与 DSL 聚合器里的 ``["1779672287769", "result"]`` 逐字对应。
- 深度清洗的提示词正文很长（2700+ 字），外置为资源文件（§6.5）。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node


# ---- DSL 节点 1779671149560 的正文（逐字符，仅函数名不同）
import json
import re

# ===== 可按需调整 =====
SKIP_EMPTY_CONTENT = True       # 是否跳过空消息
INCLUDE_DIALOG_TIME = True      # Dialog标题中是否带 create_time
MERGE_CONSECUTIVE = False       # 是否合并连续同一说话人的消息
MAX_CONTENT_LEN = 2000          # 单条消息最大长度，防止异常超长


def clean_chat_history(json_str) -> dict:
    """
    Dify代码节点入口。

    输入:
        json_str: String，原始聊天记录JSON字符串；也兼容 Object / Array。

    输出:
        result: String，清洗后的Markdown对话文本。
    """
    try:
        records = _parse_records(json_str)
        markdown = _records_to_markdown(records)

        if not markdown.strip():
            markdown = "未解析到有效对话内容。"

        return {
            "result": markdown
        }

    except Exception as e:
        return {
            "result": "聊天记录解析失败：" + str(e)
        }


# =========================
# 1. 总解析入口
# =========================

def _parse_records(value):
    """
    将输入解析为 records 列表:
    [
      {
        "create_time": "...",
        "log_time": "...",
        "messages": [
          {"speaker": "客户", "content": "...", "start": "...", "end": "..."}
        ]
      }
    ]
    """
    if value is None:
        return []

    # Dify上游如果直接传 Array
    if isinstance(value, list):
        return _parse_valid_records(value)

    # Dify上游如果直接传 Object
    if isinstance(value, dict):
        for key in ("data", "records", "list", "items"):
            if isinstance(value.get(key), list):
                return _parse_valid_records(value.get(key))
        return _parse_valid_records([value])

    # 字符串输入
    text = str(value).strip()
    if not text:
        return []

    text = _strip_code_fence(text)

    # 优先尝试标准 JSON 解析
    parsed = _try_json_loads(text)
    if parsed is not None:
        if isinstance(parsed, str):
            parsed2 = _try_json_loads(parsed.strip())
            if parsed2 is not None:
                return _parse_records(parsed2)
            return _parse_malformed_records(parsed)

        return _parse_records(parsed)

    # 标准 JSON 失败后，进入容错解析
    return _parse_malformed_records(text)


# =========================
# 2. 标准 JSON records 解析
# =========================

def _parse_valid_records(records):
    result = []

    for rec in records:
        if not isinstance(rec, dict):
            continue

        dialogue_content = (
            rec.get("dialogue_content")
            or rec.get("dialogue")
            or rec.get("dialogueContent")
            or rec.get("messages")
            or rec.get("content")
        )

        messages = _parse_messages(dialogue_content)

        # 如果当前 record 本身就是一条消息
        if not messages and ("speaker" in rec and "content" in rec):
            one = _normalize_message(rec)
            if one:
                messages = [one]

        result.append({
            "create_time": _safe_str(rec.get("create_time", "")),
            "log_time": _safe_str(rec.get("log_time", "")),
            "chat_deadline": _safe_str(rec.get("chat_deadline", "")),
            "record_type": _safe_str(rec.get("record_type", "")),
            "messages": messages
        })

    return result


def _parse_messages(value):
    """
    解析 dialogue_content：
    - 可能是 list[dict]
    - 可能是合法 JSON 字符串 '[{"content":...}]'
    - 也可能是非严格 JSON 字符串
    """
    if value is None:
        return []

    if isinstance(value, list):
        return _normalize_messages(value)

    if isinstance(value, dict):
        for key in ("messages", "data", "items", "list"):
            if isinstance(value.get(key), list):
                return _normalize_messages(value.get(key))

        msg = _normalize_message(value)
        return [msg] if msg else []

    text = str(value).strip()
    if not text:
        return []

    text = _strip_code_fence(text)

    parsed = _try_json_loads(text)
    if parsed is not None:
        return _parse_messages(parsed)

    return _extract_messages_by_regex(text)


# =========================
# 3. 非严格 JSON 容错解析
# =========================

def _parse_malformed_records(text):
    """
    处理类似这种情况：

    "dialogue_content": "[{"content":"你好","speaker":"客户"}]"

    内层 JSON 没有正确转义，导致整体不是合法 JSON。
    """
    records = []

    create_times = re.findall(r'"create_time"\s*:\s*"([^"]*)"', text, flags=re.S)
    log_times = re.findall(r'"log_time"\s*:\s*"([^"]*)"', text, flags=re.S)
    chat_deadlines = re.findall(r'"chat_deadline"\s*:\s*"([^"]*)"', text, flags=re.S)

    # 捕获每个 dialogue_content 内部数组
    block_pattern = re.compile(
        r'"dialogue_content"\s*:\s*"\[(.*?)\]"\s*(?=,?\s*\})',
        flags=re.S
    )

    blocks = [m.group(1) for m in block_pattern.finditer(text)]

    # 如果没有找到 dialogue_content，则尝试从全文中直接提取消息
    if not blocks:
        messages = _extract_messages_by_regex(text)
        if messages:
            return [{
                "create_time": "",
                "log_time": "",
                "chat_deadline": "",
                "record_type": "",
                "messages": messages
            }]
        return []

    for i, block in enumerate(blocks):
        messages = _extract_messages_by_regex(block)
        records.append({
            "create_time": create_times[i] if i < len(create_times) else "",
            "log_time": log_times[i] if i < len(log_times) else "",
            "chat_deadline": chat_deadlines[i] if i < len(chat_deadlines) else "",
            "record_type": "",
            "messages": messages
        })

    return records


def _extract_messages_by_regex(text):
    """
    从非严格 JSON 片段中提取消息对象。
    尽量兼容字段顺序变化。
    """
    messages = []

    # 聊天消息通常是扁平对象，不含嵌套花括号
    obj_pattern = re.compile(
        r'\{[^{}]*"content"[^{}]*"speaker"[^{}]*\}',
        flags=re.S
    )

    for m in obj_pattern.finditer(text):
        obj_text = m.group(0)

        content = _extract_string_field(obj_text, "content")
        speaker = _extract_string_field(obj_text, "speaker")
        start = _extract_string_field(obj_text, "start")
        end = _extract_string_field(obj_text, "end")

        msg = _normalize_message({
            "content": content,
            "speaker": speaker,
            "start": start,
            "end": end
        })

        if msg:
            messages.append(msg)

    return messages


def _extract_string_field(obj_text, field_name):
    pattern = re.compile(
        r'"' + re.escape(field_name) + r'"\s*:\s*"((?:\\.|[^"\\])*)"',
        flags=re.S
    )
    m = pattern.search(obj_text)
    if not m:
        return ""
    return _unescape_json_string(m.group(1))


# =========================
# 4. 标准化与 Markdown 输出
# =========================

def _normalize_messages(items):
    messages = []
    for item in items:
        msg = _normalize_message(item)
        if msg:
            messages.append(msg)
    return messages


def _normalize_message(item):
    if not isinstance(item, dict):
        return None

    content = _safe_str(item.get("content", "")).strip()
    speaker = _normalize_speaker(_safe_str(item.get("speaker", "")).strip())

    if SKIP_EMPTY_CONTENT and not content:
        return None

    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN] + "……"

    return {
        "speaker": speaker or "未知",
        "content": _clean_content(content),
        "start": _safe_str(item.get("start", "")),
        "end": _safe_str(item.get("end", ""))
    }


def _normalize_speaker(speaker):
    if not speaker:
        return "未知"

    if "客户" in speaker or "用户" in speaker or "顾客" in speaker:
        return "客户"

    if "销售" in speaker or "顾问" in speaker or "客服" in speaker or "坐席" in speaker:
        return "销售"

    return speaker


def _records_to_markdown(records):
    lines = []
    valid_dialog_index = 0

    for rec in records:
        messages = rec.get("messages", [])
        if not messages:
            continue

        valid_dialog_index += 1

        title = "### Dialog{}：".format(valid_dialog_index)
        create_time = rec.get("create_time", "")

        if INCLUDE_DIALOG_TIME and create_time:
            title += "（{}）".format(create_time)

        lines.append(title)

        if MERGE_CONSECUTIVE:
            messages = _merge_consecutive_messages(messages)

        for msg in messages:
            speaker = msg.get("speaker", "未知")
            content = msg.get("content", "")

            if SKIP_EMPTY_CONTENT and not content:
                continue

            lines.append("{}：{}".format(speaker, content))

        lines.append("")

    return "\n".join(lines).strip()


def _merge_consecutive_messages(messages):
    if not messages:
        return []

    merged = []

    for msg in messages:
        if merged and merged[-1]["speaker"] == msg["speaker"]:
            merged[-1]["content"] = merged[-1]["content"] + " / " + msg["content"]
            if msg.get("end"):
                merged[-1]["end"] = msg.get("end")
        else:
            merged.append(dict(msg))

    return merged


# =========================
# 5. 工具函数
# =========================

def _try_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _strip_code_fence(text):
    text = text.strip()

    # 去掉 ```json ... ``` 或 ``` ... ```
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return text.strip()


def _safe_str(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _unescape_json_string(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return (
            s.replace(r'\"', '"')
             .replace(r"\\", "\\")
             .replace(r"\n", "\n")
             .replace(r"\t", "\t")
        )


def _clean_content(content):
    # 清理多余空白，保留中文语义
    content = content.replace("\r", " ").replace("\n", " ")
    content = re.sub(r"\s+", " ", content).strip()
    return content


# ====================================================================
# 工作流定义
# ====================================================================

WORKFLOW_ID = "chat_history_data"
DISPLAY_NAME = "（新）SubAgent - 聊天记录数据信息"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1773748103758"
END = "1773748343861"
EXTRACT = "1776328382353"
BRANCH = "1778808955884"
NO_DATA_TEMPLATE = "1778808975421"
DEEP_PROMPT = "1778809002515"
AGGREGATE = "1778809010611"
CLEANSE = "1779671149560"
DEEP_LLM = "1779672287769"

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "chat_history_data"

CODE_FUNCTIONS = {
    EXTRACT: "extract_raw_payload",
    CLEANSE: "clean_chat_history",
}
"""``code`` 节点 ID → 本模块内的函数名。节点与函数的对应关系在此一处声明。"""


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result": "output"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number", "customer_data", "data_source"),
            required_phone_number=True,
            required_customer_data=True,
            required_data_source=False,
        ),
        make_node(
            EXTRACT,
            "数据提取",
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "data_source": (START, "data_source"),
            },
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_raw_payload",
            original_node_id=EXTRACT,
        ),
        make_node(
            BRANCH,
            "条件分支",
            "if-else",
            after=(EXTRACT,),
            inputs={"result": (EXTRACT, "result")},
            outputs=("result",),
            branches=(
                {
                    "id": TRUE_BRANCH,
                    "condition": {"field": "result", "operator": "not_empty"},
                    "output": "result",
                },
            ),
            else_id=ELSE_BRANCH,
            original_node_id=BRANCH,
        ),
        make_node(
            NO_DATA_TEMPLATE,
            "无数据，输出默认信息",
            "template-transform",
            after=(BRANCH,),
            on_branch=(BRANCH, "false"),
            outputs=("output",),
            template=template_name(NO_DATA_TEMPLATE),
            original_node_id=NO_DATA_TEMPLATE,
        ),
        make_node(
            CLEANSE,
            "数据初步清洗&格式转换",
            "code",
            after=(BRANCH,),
            on_branch=(BRANCH, "true"),
            inputs={"json_str": (EXTRACT, "result")},
            outputs=("result",),
            function=f"customer_profile.workflows.{SLUG}:clean_chat_history",
            original_node_id=CLEANSE,
        ),
        make_node(
            DEEP_PROMPT,
            "数据深度清洗Prompt",
            "template-transform",
            after=(CLEANSE,),
            inputs={"dialog_markdown": (CLEANSE, "result")},
            outputs=("output",),
            template=template_name(DEEP_PROMPT),
            original_node_id=DEEP_PROMPT,
        ),
        make_node(
            DEEP_LLM,
            "数据深度清洗 工具",
            "llm",
            # 有意差异 W2/W3：DSL 的 gemini_retry_2_times 工具节点归一为一次 llm_call
            after=(DEEP_PROMPT,),
            inputs={"input_prompt": (DEEP_PROMPT, "output")},
            outputs=("result",),
            output="result",
            system_text="You are a helpful AI assistant.",
            original_node_id=DEEP_LLM,
        ),
        make_node(
            AGGREGATE,
            "变量聚合器",
            "variable-aggregator",
            after=(DEEP_LLM, NO_DATA_TEMPLATE),
            variables=((DEEP_LLM, "result"), (NO_DATA_TEMPLATE, "output")),
            outputs=("output",),
            original_node_id=AGGREGATE,
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(AGGREGATE,),
            inputs={"result": (AGGREGATE, "output")},
            outputs=("result",),
            original_node_id=END,
        ),
    ),
)


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": "MengShiCustomerChatInfo",
    }


# ====================================================================
# code 节点 -> 函数名映射
#
# 迁移只允许改函数名，不许改函数体。跨节点重名的辅助函数（同一份 DSL 里不同
# 节点各写了一份 ``_parse_json`` 之类）必须改名，否则后一份会覆盖前一份。
# 这里如实记录每个节点用了什么名字，``tests/test_code_verbatim.py`` 按它把
# DSL 原文里的旧名换成新名后再逐字符比对——差异因此只剩下「名字」。
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    '1779671149560': {'main': 'clean_chat_history', 'renames': {}},
}
