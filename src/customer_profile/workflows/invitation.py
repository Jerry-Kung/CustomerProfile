"""客户线索邀约：取数、录音、三路生成、确定性合并与回写。"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from ..definitions import WorkflowDef, make_node
from ..invitation_contract import (
    AUDIT_ITEMS,
    InvitationInputs,
    output_schema,
    validate_audit,
    validate_follow_up,
    validate_script,
)

WORKFLOW_ID = "invitation_strategy"
MODULE = "customer_profile.workflows.invitation"
START = "invitation_start"
REQUEST = "invitation_request"
PROFILE = "invitation_profile"
PROFILE_PARSE = "invitation_profile_parse"
HISTORY = "invitation_history"
AUDIO = "invitation_audio"
CONTEXT = "invitation_communication_context"
ANALYSIS_BRANCH = "invitation_recording_analysis_branch"
NO_AUDIO_ANALYSIS = "invitation_no_recording_analysis"
AUDIT_RESULT = "invitation_audit_result"
FOLLOW_UP_RESULT = "invitation_follow_up_result"
STANDARD = "invitation_assessment_standard"
AUDIT = "invitation_audit"
FOLLOW_UP = "invitation_follow_up"
BRANCH = "invitation_first_or_followup"
FIRST = "invitation_script_first"
FOLLOWUP = "invitation_script_followup"
SCRIPT = "invitation_script"
MERGE = "invitation_merge"
WRITE = "invitation_writeback"
CONFIRM = "invitation_confirm_delivery"
END = "invitation_end"
NO_RECORDING = "该用户不存在外呼录音数据"
INVALID_RECORDING = "该录音文件无有效内容"
RECORDING_CHANNEL = "OntologyManualOutboundCallRecordingFile"
LEGACY_RECORDING_CHANNEL = "AIOutboundCallRecordingFile"


def prepare_request(
    phone_number: str,
    batch_id: str,
    work_env: str = "test",
    execution_id: str | None = None,
) -> dict:
    data = InvitationInputs.model_validate(
        dict(
            phone_number=phone_number,
            batch_id=batch_id,
            work_env=work_env,
            execution_id=execution_id,
        )
    ).model_dump()
    return {
        **data,
        "service": "mhero" if data["work_env"] == "production" else "profile",
        "as_of": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }


def parse_profile(body: str) -> dict:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("画像接口须返回 JSON 对象")
    return {"customer_profile": json.dumps(value, ensure_ascii=False)}


def select_recordings(records: str, batch_id: str) -> dict:
    """按通话匹配本体录音明细；兼容旧渠道，不跨通话合并历史录音包。"""
    data = json.loads(records)
    if not isinstance(data, list):
        raise ValueError("历史资料接口须返回 JSON 数组；取数异常不能当作无录音")
    candidates = [
        row
        for row in data
        if isinstance(row, dict)
        and row.get("channel") in (RECORDING_CHANNEL, LEGACY_RECORDING_CHANNEL)
        and row.get("is_valid", 1) not in (0, "0")
    ]
    if not candidates:
        return {"files": [], "updated_at": "", "record_id": None, "record_ids": []}

    def identity(row):
        payload = row.get("raw_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("录音 raw_payload 须为对象")
        call_id = payload.get("manual_outbound_call_id") or payload.get(
            "source_business_id"
        )
        return (
            str(row.get("batch_id") or "").strip(),
            str(call_id or "").strip(),
        )

    identified = [(row, *identity(row)) for row in candidates]
    # 业务 batch_id 优先表示通话 ID；画像批次可能由多通电话共享，不能据此混合。
    matching = [row for row, _, call in identified if call == batch_id]
    if not matching:
        batch_matches = [
            (row, call) for row, batch, call in identified if batch == batch_id
        ]
        matching = [row for row, _ in batch_matches]
        calls = {call for _, call in batch_matches if call}
        if len(calls) > 1:
            raise ValueError("邀约批次对应多通外呼，须使用明确的通话 ID 重新触发")
        if calls:
            matching = [row for row, _, call in identified if call in calls]
    if not matching and any(batch or call for _, batch, call in identified):
        raise ValueError("邀约批次对应的有效外呼录音已不存在，须核对业务批次后重新触发")
    if not matching and any(row["channel"] == RECORDING_CHANNEL for row in candidates):
        raise ValueError("本体外呼录音缺少批次和通话 ID，无法确认本次邀约对应的录音")
    # M2M 历史资料按 created_at 升序返回；id 是业务侧选择最新录音的排序键。
    _, selected = max(
        enumerate(matching or candidates),
        key=lambda indexed: (int(indexed[1].get("id") or 0), indexed[0]),
    )
    # 新渠道每条资料只存一个录音；旧渠道可能把多次通话累计到同一个包中。
    # 同一通话存在正式明细时只用明细，不能把旧包中其它通话的 URL 一并带入。
    details = [row for row in matching if row["channel"] == RECORDING_CHANNEL]
    selected_rows = sorted(details, key=lambda row: int(row.get("id") or 0)) or [
        selected
    ]
    selected = selected_rows[-1]
    urls = []
    for row in selected_rows:
        value = row.get("media_urls") or []
        if isinstance(value, str):
            value = json.loads(value) if value.lstrip().startswith("[") else [value]
        if not isinstance(value, list) or any(
            not isinstance(url, str) for url in value
        ):
            raise ValueError("录音 media_urls 须为 URL 字符串或字符串数组")
        urls.extend(value)
    files = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    if any(not url.startswith(("https://", "http://")) for url in files):
        raise ValueError("录音 URL 须使用 HTTP 或 HTTPS")
    return {
        "files": files,
        "updated_at": str(selected.get("updated_at") or ""),
        "record_id": selected.get("id"),
        "record_ids": [row["id"] for row in selected_rows if row.get("id") is not None],
    }


def combine_recordings(parts: list[str], files: list[str], updated_at: str) -> dict:
    if len(parts) != len(files) or any(
        not isinstance(part, str) or not part.strip() for part in parts
    ):
        raise ValueError("录音转写结果不完整")
    return {
        "result": "\n".join(parts) if files else NO_RECORDING,
        "update_time": updated_at if files else "",
        "has_recording": bool(files),
    }


def no_recording_analysis(has_recording: bool) -> dict:
    """缺少录音时不调用质检、待办模型；固定结构兼容现有业务展示。"""
    if has_recording is not False:
        raise ValueError("有录音时不能跳过录音分析")
    audit = {
        "overall_score": 0,
        "overall_rating": "未质检",
        "rating_description": "本次无外呼录音，未执行话术质检。",
        "dimensions": [
            {
                "key": key,
                "name": name,
                "score": 0,
                "items": [
                    {
                        "order": i,
                        "name": item,
                        "result": "partial",
                        "highlights": [],
                        "issues": [],
                    }
                    for i, item in enumerate(items, 1)
                ],
            }
            for key, (name, items) in AUDIT_ITEMS.items()
        ],
    }
    return {
        "audit_info": json.dumps({"audit": audit}, ensure_ascii=False),
        "follow_up_info": json.dumps(
            {
                "follow_up_summary": "无外呼录音，未提取通话待办",
                "follow_up_items": [],
            },
            ensure_ascii=False,
        ),
    }


def assemble_result(
    phone_number: str,
    batch_id: str,
    execution_id: str | None,
    audit_info: str,
    script_info: str,
    follow_up_info: str,
    update_time: str,
    has_recording: bool = True,
) -> dict:
    audit = validate_audit(json.loads(audit_info))["audit"]
    script = validate_script(json.loads(script_info))["script"]
    follow_up = validate_follow_up(json.loads(follow_up_info))
    if not has_recording:
        # “无录音”不是销售表现不合格；状态由真实文件分支决定，不接受模型自报跳检。
        empty = no_recording_analysis(False)
        audit = json.loads(empty["audit_info"])["audit"]
        follow_up = json.loads(empty["follow_up_info"])
    # 无录音不虚构上传时间；有时间时按上海时区展示。
    formatted = ""
    if update_time:
        stamp = datetime.fromisoformat(update_time.replace("Z", "+00:00"))
        if stamp.tzinfo:
            stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
        formatted = stamp.strftime("%Y-%m-%d %H:%M")
    audit["recording_label"] = f"外呼录音 · {formatted}" if formatted else "外呼录音"
    result = {
        "phone": phone_number,
        "batch_id": batch_id,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        ),
        **follow_up,
        "script": script,
        "audit": audit,
    }
    if execution_id is not None:
        result["execution_id"] = execution_id
    return {"result": json.dumps(result, ensure_ascii=False)}


async def confirm_delivery(
    status: int, result: str, service: str, *, http=None, settings=None, node_ref=None
) -> dict:
    """HTTP 200 不代表本次结果已入库；业务侧会对过期执行返回空成功。"""
    if status == 0:
        return {"delivery": "skipped"}
    if http is None or settings is None:
        raise RuntimeError("邀约回写核对未注入 HTTP 客户端")
    payload = json.loads(result)
    attempt = await http.request(
        "GET",
        f"/api/v1/remote/data/invitation/{payload['phone']}/status",
        service=service,
        headers={"X-API-Key": settings.api_key_for(service)},
        node_ref=node_ref,
    )
    state = json.loads(attempt.response_text or "")
    expected = (payload["phone"], payload["batch_id"], payload.get("execution_id"))
    actual = (state.get("phone"), state.get("batch_id"), state.get("execution_id"))
    if (
        actual != expected
        or state.get("analysis_status") != 0
        or not state.get("has_script")
        or not state.get("has_audit")
    ):
        raise ValueError(
            "INVITATION_REQUIRES_RECONCILIATION: 回写后的业务执行身份或结果状态不匹配"
        )
    return {"delivery": "confirmed"}


def _http(
    node_id: str,
    title: str,
    path: str,
    *,
    after: tuple[str, ...],
    method: str = "get",
    body=None,
):
    inputs = {"service": (REQUEST, "service")}
    if "{{#" in path:
        inputs["@path"] = (REQUEST, "phone_number")
    config = {
        "@service_field": "service",
        "@path": path,
        "@method": method,
        "@headers": {"accept": "application/json"},
        "@outputs": {"body": "$text", "status": "$status"},
    }
    if body:
        inputs["@body_ref"] = body
        config.update({"@writeback": True, "@body_ref": True, "@retry_max": 0})
    return make_node(
        node_id,
        title,
        "http-request",
        after=after,
        inputs=inputs,
        outputs=("body", "status"),
        **config,
    )


def _llm(
    node_id: str,
    title: str,
    template: str,
    validator: str,
    *,
    after: tuple[str, ...],
    inputs: dict,
    on_branch=None,
):
    return make_node(
        node_id,
        title,
        "llm",
        after=after,
        inputs=inputs,
        outputs=("text", "parsed_json"),
        template=f"invitation__{template}",
        expect_json=True,
        response_validator=(
            f"customer_profile.invitation_context:{validator}"
            if validator == "validate_personalized_script"
            else f"customer_profile.invitation_contract:{validator}"
        ),
        response_schema=output_schema(validator),
        **(
            {"response_context_field": "communication_context"}
            if validator == "validate_personalized_script"
            else {}
        ),
        system_text="You are a helpful AI assistant.",
        on_branch=on_branch,
    )


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name="客户线索邀约",
    source_dsl="客户线索邀约.yml（2026-10-29）",
    entries=(START,),
    exits=(END,),
    outputs={"result": "result", "delivery": "delivery"},
    nodes=(
        make_node(
            START,
            "邀约任务输入",
            "start",
            variables=("phone_number", "batch_id", "work_env", "execution_id"),
            defaults={"work_env": "test", "execution_id": None},
            outputs=("phone_number", "batch_id", "work_env", "execution_id"),
        ),
        make_node(
            REQUEST,
            "校验业务身份与环境",
            "code",
            after=(START,),
            inputs={
                key: (START, key)
                for key in ("phone_number", "batch_id", "work_env", "execution_id")
            },
            outputs=(
                "phone_number",
                "batch_id",
                "work_env",
                "execution_id",
                "service",
                "as_of",
            ),
            function=f"{MODULE}:prepare_request",
        ),
        _http(
            PROFILE,
            "获取当前客户画像",
            "/api/v1/remote/data/profile/{{#invitation_request.phone_number#}}",
            after=(REQUEST,),
        ),
        make_node(
            PROFILE_PARSE,
            "校验画像响应",
            "code",
            after=(PROFILE,),
            inputs={"body": (PROFILE, "body")},
            outputs=("customer_profile",),
            function=f"{MODULE}:parse_profile",
        ),
        _http(
            HISTORY,
            "获取有效历史资料",
            "/api/v1/remote/data/history/{{#invitation_request.phone_number#}}",
            after=(REQUEST,),
        ),
        make_node(
            AUDIO,
            "外呼录音原始数据解析",
            "tool",
            after=(HISTORY,),
            inputs={
                "customer_data": (HISTORY, "body"),
                "batch_id": (REQUEST, "batch_id"),
            },
            outputs=("result", "update_time", "has_recording"),
            **{
                "@workflow": "invitation_outbound_audio",
                "@inputs": {"customer_data": "customer_data", "batch_id": "batch_id"},
            },
        ),
        make_node(
            CONTEXT,
            "整理本轮沟通事实与阶段",
            "code",
            after=(AUDIO, PROFILE_PARSE),
            inputs={
                "customer_profile": (PROFILE_PARSE, "customer_profile"),
                "records": (HISTORY, "body"),
                "transcript": (AUDIO, "result"),
                "has_recording": (AUDIO, "has_recording"),
                "batch_id": (REQUEST, "batch_id"),
                "as_of": (REQUEST, "as_of"),
            },
            outputs=("communication_context", "has_previous_contact", "stage_hint"),
            function="customer_profile.invitation_context:prepare_context",
        ),
        make_node(
            ANALYSIS_BRANCH,
            "有录音才调用质检与待办模型",
            "if-else",
            after=(AUDIO,),
            inputs={"has_recording": (AUDIO, "has_recording")},
            branches=(
                {
                    "id": "recorded",
                    "condition": {
                        "field": "has_recording",
                        "operator": "eq",
                        "value": True,
                    },
                },
            ),
            else_id="missing",
        ),
        make_node(
            NO_AUDIO_ANALYSIS,
            "无录音：未质检与空待办",
            "code",
            after=(ANALYSIS_BRANCH,),
            inputs={"has_recording": (AUDIO, "has_recording")},
            outputs=("audit_info", "follow_up_info"),
            function=f"{MODULE}:no_recording_analysis",
            on_branch=(ANALYSIS_BRANCH, "missing"),
        ),
        make_node(
            STANDARD,
            "外呼话术考核要点",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template="invitation__1780380703102",
        ),
        _llm(
            AUDIT,
            "销售话术检核",
            "1780453998655",
            "validate_audit",
            after=(ANALYSIS_BRANCH, STANDARD),
            inputs={
                "call_transcript": (AUDIO, "result"),
                "assessment_standard": (STANDARD, "output"),
            },
            on_branch=(ANALYSIS_BRANCH, "recorded"),
        ),
        _llm(
            FOLLOW_UP,
            "通话总结与待办抽取",
            "17821167995750",
            "validate_follow_up",
            after=(ANALYSIS_BRANCH,),
            inputs={"call_transcript": (AUDIO, "result")},
            on_branch=(ANALYSIS_BRANCH, "recorded"),
        ),
        make_node(
            AUDIT_RESULT,
            "质检结果",
            "variable-aggregator",
            after=(AUDIT, NO_AUDIO_ANALYSIS),
            variables=((AUDIT, "text"), (NO_AUDIO_ANALYSIS, "audit_info")),
            outputs=("output",),
        ),
        make_node(
            FOLLOW_UP_RESULT,
            "通话待办结果",
            "variable-aggregator",
            after=(FOLLOW_UP, NO_AUDIO_ANALYSIS),
            variables=((FOLLOW_UP, "text"), (NO_AUDIO_ANALYSIS, "follow_up_info")),
            outputs=("output",),
        ),
        make_node(
            BRANCH,
            "选择首邀或二次邀约",
            "if-else",
            after=(CONTEXT,),
            inputs={"has_previous_contact": (CONTEXT, "has_previous_contact")},
            branches=(
                {
                    "id": "first",
                    "condition": {
                        "field": "has_previous_contact",
                        "operator": "eq",
                        "value": False,
                    },
                },
            ),
            else_id="followup",
        ),
        _llm(
            FIRST,
            "首次邀约策略与话术",
            "1780473244130",
            "validate_personalized_script",
            after=(BRANCH,),
            inputs={"communication_context": (CONTEXT, "communication_context")},
            on_branch=(BRANCH, "first"),
        ),
        _llm(
            FOLLOWUP,
            "二次邀约策略与话术",
            "17821186748980",
            "validate_personalized_script",
            after=(BRANCH,),
            inputs={
                "communication_context": (CONTEXT, "communication_context"),
            },
            on_branch=(BRANCH, "followup"),
        ),
        make_node(
            SCRIPT,
            "邀约话术结果",
            "variable-aggregator",
            after=(FIRST, FOLLOWUP),
            variables=((FIRST, "text"), (FOLLOWUP, "text")),
            outputs=("output",),
        ),
        make_node(
            MERGE,
            "生成邀约回写结果",
            "code",
            after=(AUDIT_RESULT, FOLLOW_UP_RESULT, SCRIPT),
            inputs={
                "phone_number": (REQUEST, "phone_number"),
                "batch_id": (REQUEST, "batch_id"),
                "execution_id": (REQUEST, "execution_id"),
                "audit_info": (AUDIT_RESULT, "output"),
                "script_info": (SCRIPT, "output"),
                "follow_up_info": (FOLLOW_UP_RESULT, "output"),
                "update_time": (AUDIO, "update_time"),
                "has_recording": (AUDIO, "has_recording"),
            },
            outputs=("result",),
            function=f"{MODULE}:assemble_result",
        ),
        _http(
            WRITE,
            "回写邀约分析",
            "/api/v1/remote/callback/update-invitation",
            after=(MERGE,),
            method="post",
            body=(MERGE, "result"),
        ),
        make_node(
            CONFIRM,
            "核对业务侧落库结果",
            "code",
            after=(WRITE,),
            inputs={
                "status": (WRITE, "status"),
                "result": (MERGE, "result"),
                "service": (REQUEST, "service"),
            },
            outputs=("delivery",),
            function=f"{MODULE}:confirm_delivery",
        ),
        make_node(
            END,
            "邀约分析输出",
            "end",
            after=(CONFIRM,),
            inputs={"result": (MERGE, "result"), "delivery": (CONFIRM, "delivery")},
            outputs=("result", "delivery"),
        ),
    ),
)
