"""将现有业务资料整理为本轮沟通依据，不新增模型调用或补造客户事实。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .invitation_contract import validate_script

# 只选择购车与沟通有关的信息，不把收入、住址、年龄等人设信息交给对客话术。
NOTE_FIELDS = {
    "customer_name": "客户称呼",
    "title": "称谓",
    "budget": "客户预算",
    "budget_range": "客户预算",
    "intended_model": "意向车型",
    "competing_brand": "关注的竞品品牌",
    "competing_model": "关注的竞品车型",
    "viewing_progress": "看车进度",
    "test_driven_models": "试驾经历",
    "planned_purchase_time": "购车时间意向",
    "purchase_purpose": "用车用途",
    "purchase_type": "购车类型",
    "core_concerns": "核心关注",
    "pain_points": "待解决问题",
    "brand_concerns": "品牌顾虑",
    "current_vehicle_info": "现有车辆",
    "trade_in_model": "置换车型",
    "trade_in_requirement": "置换诉求",
    "payment_method": "付款意向",
    "decision_maker": "购车决策参与人",
    "contact_preference": "沟通方式偏好",
    "communication_rhythm": "沟通节奏",
    "follow_up_frequency": "联系频率意向",
    "seating_requirement": "乘坐需求",
    "driving_preference": "驾驶偏好",
    "optional_config": "配置关注",
    "special_requirements": "其它明确需求",
}
HISTORY_FIELDS = {
    "OntologyManualOutboundCall": (
        "人工外呼记录",
        ("call_time", "call_status", "call_duration"),
    ),
    "OntologyTestDriveSchedule": (
        "试驾预约与履约记录",
        (
            "schedule_time",
            "test_drive_date",
            "start_time",
            "end_time",
            "status",
            "order_status",
        ),
    ),
}
UNKNOWN = {"", "未知", "待识别", "待确认", "未提供", "暂无", "无", "null", "None"}
PRIOR_CONTACT = re.compile(
    r"已(?:完成)?(?:试驾|到店|下订|订车|购车|成交|交付)|(?:试驾|到店)后|试驾录音中|通话中客户|客户(?:主动)?询问[“\"]|客户(?:说|表示)[“\"]"
)
NO_CONTACT = re.compile(
    r"无(?:任何)?(?:有效)?沟通|无(?:任何)?(?:通话|聊天|到店)记录|尚未建立沟通|待首次有效接触"
)
PRIVATE_EVIDENCE = re.compile(r"年收入|收入档|消费档|住址|年龄|号段|第三方画像")


def _reported_contact(pieces) -> bool:
    for piece in pieces:
        for clause in re.split(r"[，。；;]", _text(piece)):
            if re.search(
                r"(?:未|无|没有|缺少|尚未).{0,6}(?:试驾|到店|通话|沟通|互动|接触|联系|下订|订车|购车|成交|交付)",
                clause,
            ):
                continue
            if PRIOR_CONTACT.search(clause):
                return True
    return False


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _payload(row) -> dict:
    value = row.get("raw_payload") or {}
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _timestamp(value) -> float:
    try:
        date = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        return date.replace(tzinfo=date.tzinfo or ZoneInfo("Asia/Shanghai")).timestamp()
    except ValueError:
        return 0


def prepare_context(
    customer_profile: str,
    records: str,
    transcript: str,
    has_recording: bool,
    batch_id: str,
    as_of: str,
) -> dict:
    profile = json.loads(customer_profile)
    history = json.loads(records)
    if not isinstance(profile, dict) or not isinstance(history, list):
        raise ValueError("邀约沟通资料须包含画像对象和历史资料数组")
    if not isinstance(has_recording, bool):
        raise ValueError("录音是否存在须由文件选择结果明确给出")

    facts = []

    def add(label, value, source, detail=None, *, confirmed=False, feedback=None):
        value = _text(value)
        if value in UNKNOWN or value.startswith("暂无"):
            return
        detail = detail if isinstance(detail, dict) else {}
        evidence = detail.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [evidence]
        facts.append(
            {
                "id": f"F{len(facts) + 1}",
                "label": label,
                "value": value,
                "basis": "人工确认" if confirmed else "画像归纳，非客户原话",
                "source": source,
                "evidence": [
                    _text(e)
                    for e in evidence
                    if _text(e) and not PRIVATE_EVIDENCE.search(_text(e))
                ],
                "confidence": detail.get("confidence"),
                "feedback": feedback,
            }
        )

    notes = profile.get("basic_notes") or {}
    if isinstance(notes, dict):
        for key, label in NOTE_FIELDS.items():
            note = notes.get(key)
            if not isinstance(note, dict):
                continue
            detail = note.get("reasoning_detail") or {}
            if not detail and note.get("reasoning_summary"):
                detail = {"evidence": [note["reasoning_summary"]]}
            add(
                label,
                note.get("value"),
                f"basic_notes.{key}",
                detail,
                confirmed=note.get("is_locked") in (True, 1, "1"),
                feedback=note.get("ai_feedback"),
            )

    for section in ("purchase_decisions",):
        groups = profile.get(section) or []
        if not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            for ii, item in enumerate(group.get("items") or []):
                if isinstance(item, dict):
                    add(
                        group.get("category") or "购车关注",
                        item.get("label"),
                        f"{section}.{gi}.items.{ii}",
                        item.get("reasoning_detail"),
                        feedback=item.get("ai_feedback"),
                    )

    suggested_next_steps = []
    battle = profile.get("deal_battle") or {}
    if isinstance(battle, dict):
        for key in ("win_conditions", "loss_conditions"):
            for i, item in enumerate(battle.get(key) or []):
                if isinstance(item, dict) and _text(item.get("label")) not in UNKNOWN:
                    suggested_next_steps.append(
                        {
                            "label": item["label"],
                            "basis": "画像建议，非已发生事实",
                            "source": f"deal_battle.{key}.{i}",
                        }
                    )

    # 画像的阶段说明用于发现冲突，不把“高意向”“谨慎”等标签直接说给客户。
    intent = (profile.get("ai_insight") or {}).get("purchase_intent_level") or {}
    detail = intent.get("reasoning_detail") or {}
    stage_report = {
        "summary": "。".join(
            part
            for part in re.split(r"[。；]", _text(detail.get("summary")))
            if part and not PRIVATE_EVIDENCE.search(part)
        ),
        "evidence": [
            e
            for e in (detail.get("evidence") or [])
            if not PRIVATE_EVIDENCE.search(_text(e))
        ],
    }
    psychology = profile.get("decision_psychology") or []
    if isinstance(psychology, list):
        for i, item in enumerate(psychology):
            if isinstance(item, dict) and item.get("key") in (
                "current_attitude",
                "communication_preference",
            ):
                add(
                    item.get("label") or item["key"],
                    item.get("value"),
                    f"decision_psychology.{i}",
                    item.get("reasoning_detail"),
                    feedback=item.get("ai_feedback"),
                )

    events = []
    call_time = ""
    call_time_rank = (-1, 0)
    call_time_source = None
    for row in history:
        if not isinstance(row, dict) or row.get("is_valid", 1) in (0, "0"):
            continue
        if row.get("channel") not in (
            *HISTORY_FIELDS,
            "OntologyManualOutboundCallRecordingFile",
            "AIOutboundCallRecordingFile",
        ):
            continue
        payload = _payload(row)
        identities = {
            _text(row.get("batch_id")),
            _text(payload.get("manual_outbound_call_id")),
            _text(payload.get("source_business_id")),
            _text(payload.get("source_id")),
        }
        if batch_id in identities and payload.get("call_time"):
            # 明确通话 ID 优先于可能被多通电话共享的画像批次，与录音选择保持一致。
            exact_call = batch_id in {
                _text(payload.get(key))
                for key in (
                    "manual_outbound_call_id",
                    "source_business_id",
                    "source_id",
                )
            }
            rank = (2 if exact_call else 1, _timestamp(payload["call_time"]))
            if rank > call_time_rank:
                call_time = _text(payload["call_time"])
                call_time_rank = rank
                call_time_source = row.get("id")
        if row.get("channel") not in HISTORY_FIELDS:
            continue
        label, fields = HISTORY_FIELDS[row["channel"]]
        values = {
            key: payload[key] for key in fields if payload.get(key) not in (None, "")
        }
        event_time = next(
            (
                _text(payload[k])
                for k in ("call_time", "end_time", "start_time", "schedule_time")
                if payload.get(k)
            ),
            "",
        )
        events.append(
            {
                "source_record_id": row.get("id"),
                "kind": label,
                "time": event_time,
                "fields": values,
            }
        )
    events.sort(key=lambda event: _timestamp(event["time"]), reverse=True)
    events = events[:5]

    valid_dialogue = has_recording and bool(
        transcript.replace("该录音文件无有效内容", "").strip()
    )
    stage_text = json.dumps(stage_report, ensure_ascii=False)
    contact_pieces = [stage_report["summary"], *stage_report["evidence"]]
    contact_pieces.extend(
        fact["value"]
        for fact in facts
        if fact["label"] in ("看车进度", "试驾经历", "当前态度")
    )
    def known_trial(fact):
        return fact["label"] == "试驾经历" and not re.match(
            r"^(?:未|没|无|待|计划|意向|暂无|不确定)", fact["value"]
        )
    reported_trial = any(known_trial(fact) for fact in facts)
    manually_confirmed_trial = any(
        known_trial(fact) and fact["basis"] == "人工确认" for fact in facts
    )
    manually_confirmed_contact = manually_confirmed_trial or _reported_contact(
        fact["value"]
        for fact in facts
        if fact["basis"] == "人工确认"
        and fact["label"] in ("看车进度", "试驾经历", "当前态度")
    )
    profile_contact = manually_confirmed_contact or (
        (_reported_contact(contact_pieces) or reported_trial)
        and not NO_CONTACT.search(stage_text)
    )
    connected = any(
        event["kind"] == "人工外呼记录"
        and _text(event["fields"].get("call_status")).lower()
        in ("接通", "已接通", "通话成功", "answered", "connected")
        and _text(event["fields"].get("call_duration")).replace(".", "", 1).isdigit()
        and float(event["fields"]["call_duration"]) > 0
        for event in events
    )
    has_previous_contact = bool(has_recording or events or profile_contact)
    if valid_dialogue:
        stage_hint = "已有本次有效通话，优先承接客户最新限制与未完成事项"
    elif events or profile_contact:
        stage_hint = "已有业务或沟通线索，依据最新进度确认未完成事项，不仅因无录音当作首邀"
    elif has_recording:
        stage_hint = "本次录音无有效内容，不能补造沟通事实，先核对实际进度"
    else:
        stage_hint = "缺少有效接触证据，先确认真实需求与联系意愿"
    brief = {
        "as_of": as_of,
        "brand": "猛士",
        "stage_hint": stage_hint,
        "profile_version": profile.get("profile_version"),
        "can_reference_previous_contact": bool(
            valid_dialogue or connected or profile_contact
        ),
        "latest_call": {
            "has_recording": has_recording,
            "valid_content": valid_dialogue,
            "occurred_at": call_time,
            "time_source_record_id": call_time_source,
            "text": transcript if valid_dialogue else "",
        },
        "facts": sorted(facts, key=lambda fact: fact["basis"] != "人工确认"),
        "profile_stage_report": stage_report,
        "recent_business_events": events,
        "suggested_next_steps": suggested_next_steps,
        "available_store_slots": [],
        "verified_product_policies": [],
    }
    return {
        "communication_context": json.dumps(brief, ensure_ascii=False),
        "has_previous_contact": has_previous_contact,
        "stage_hint": stage_hint,
    }


def validate_personalized_script(value, *, context: str) -> dict:
    result = validate_script(value)
    brief = json.loads(context)
    script = result["script"]
    outward = "\n".join(
        [*(step["content"] for step in script["phone_steps"]), script["wechat_script"]]
    )
    if not brief["can_reference_previous_contact"] and any(
        phrase in outward
        for phrase in (
            "上次您说",
            "上次您提到",
            "上次我们",
            "您前面提到",
            "您之前说过",
            "您之前提到",
            "您前面比较关注",
            "您之前比较关注",
        )
    ):
        raise ValueError(
            "没有有效接触证据，不能把画像归纳说成客户以前明确表达过；改为首次确认问题"
        )
    if not brief["available_store_slots"] and re.search(
        r"已(?:经)?(?:帮您|给您)?预留|我给您留两个时间", outward
    ):
        raise ValueError(
            "没有门店可用时段，不得声称已预留接待时间；应先询问客户时间意向"
        )
    if re.search(
        r"(?:已经|已)(?:把.{0,15})?(?:发给您|发送给您|发到您微信)|资料(?:已经|已)(?:发|整理好|准备好)|(?:我|这边).{0,5}已经(?:整理好|准备好)",
        outward,
    ):
        raise ValueError(
            "没有资料发送或准备完成的凭据，不得声称已发送或已准备完成；只能提出后续动作"
        )
    if re.search(
        r"(?:依据|根据|事实|来源)\s*F\d+\b|basic_notes\.|reasoning_detail|available_store_slots",
        outward,
    ):
        raise ValueError("对客话术不得包含内部事实编号或字段名，应自然转述实际沟通内容")
    return result
