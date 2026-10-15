"""确定性字段注入层（``inject_deterministic``）的回归用例。

**为什么这几条必须有**：两个字段有**定义级**的确定取值——

- ``phone_number`` 的真值就是本案入参，v3-fix 实测却靠从截图（``wechat_id``）反捞，
  还被降成 ``inferred_low``；
- ``driver_license_status`` 由「到店试驾」或「有旧车 / 要置换」直接蕴含
  （没驾照不能试驾、不能开自家车），v3-fix 实测 5 案全凭 LLM 归纳、2 案留空。

注入是**代码强制**的，因此要正面钉住「该注入时注入」与「不该注入时不注入」两侧；
还要钉住接线——注入点若退回定位层**之前**，注入值会被 ``unknown_channel`` 降级，
这一处只在节点图上看得见。
"""

from __future__ import annotations

import json

from customer_profile.workflows import customer_profile_pipeline as pipe


def _records(*records):
    return json.dumps(list(records), ensure_ascii=False)


def _claims(*claims):
    return json.dumps({"fields": list(claims)}, ensure_ascii=False)


def record(source_name, content, *, status="ok", record_id=None, kind="transcript",
           source_class="primary_interaction"):
    return {
        "record_id": record_id or f"rec_{source_name}",
        "channel": source_name,
        "source_name": source_name,
        "source_class": source_class,
        "status": status,
        "created_at": "2026-09-21T11:27:16",
        "content": content,
        "content_kind": kind,
    }


def _inject(records, claims, phone="13800000000"):
    result = pipe.inject_deterministic(records, claims, phone)
    return {x["attribute"]: x for x in json.loads(result["claims_json"])["fields"]}, result


# ---------------------------------------------------------------- 手机号

def test_phone_number_injected_from_argument():
    """电话号直取入参，不再靠从截图反捞。"""
    merged, result = _inject(_records(), _claims())
    assert merged["phone_number"]["value"] == "13800000000"
    assert merged["phone_number"]["basis"] == "fact"
    assert merged["phone_number"]["record_channel"] == pipe.INJECTED_PHONE_CHANNEL
    assert result["added"] == 1


def test_phone_number_channel_is_registry_tier():
    """注入来源必须落**登记档**，否则身份字段会被弱来源规则降级。"""
    from customer_profile import projection as proj

    assert proj.CHANNEL_TIER[pipe.INJECTED_PHONE_CHANNEL] == proj.REGISTRY
    assert proj.CHANNEL_TIER[pipe.INJECTED_LICENSE_CHANNEL] == proj.REGISTRY


def test_blank_phone_number_not_injected():
    """入参为空时不注入空值占位（空值会伪装成一条有效结论）。"""
    merged, result = _inject(_records(), _claims(), phone="  ")
    assert "phone_number" not in merged
    assert result["injected"] == 0


# ---------------------------------------------------------------- 驾照

def test_test_drive_implies_license():
    """有试驾录音（且有内容）即有驾照——没驾照不能试驾。"""
    merged, result = _inject(
        _records(record("试驾录音信息", "客户：这车开着挺轻快")),
        _claims(),
    )
    assert merged["driver_license_status"]["value"] == {"key": "yes", "input_value": ""}
    assert merged["driver_license_status"]["basis"] == "fact"
    assert result["license_injected"] is True


def test_old_car_implies_license():
    """有旧车即可开车，即有驾照（与驾龄无关，不编 input_value）。"""
    merged, _ = _inject(
        _records(),
        _claims({"attribute": "old_car_model", "value": "2019款别克君威", "basis": "fact"}),
    )
    entry = merged["driver_license_status"]
    assert entry["value"] == {"key": "yes", "input_value": ""}
    # 结构化枚举：只给 key、不编 `input_value`（写死一个驾龄就是编造）。
    assert entry["value"]["input_value"] == ""


def test_trade_in_implies_license():
    """``trade_in_requirement=yes`` 本身就是「有旧车」的凭据。"""
    merged, result = _inject(
        _records(),
        _claims({"attribute": "trade_in_requirement", "value": "yes", "basis": "fact"}),
    )
    assert "driver_license_status" in merged
    assert result["license_injected"] is True


def test_no_evidence_no_license():
    """既无试驾、又无旧车时**不注入**——无凭据的确定值就是编造。"""
    merged, result = _inject(_records(), _claims())
    assert "driver_license_status" not in merged
    assert result["license_injected"] is False


def test_empty_test_drive_does_not_imply_license():
    """录音记录在、但内容为空时不构成试驾证据。"""
    merged, _ = _inject(
        _records(record("试驾录音信息", "   ")),
        _claims(),
    )
    assert "driver_license_status" not in merged


# ---------------------------------------------------------------- 同字段取舍

def test_injection_wins_at_same_basis():
    """同档（fact）时以注入为准——注入是定义级蕴含，不是猜测。"""
    merged, result = _inject(
        _records(record("试驾录音信息", "客户：我先试试这台")),
        _claims({"attribute": "driver_license_status", "value": "no", "basis": "fact"}),
    )
    assert merged["driver_license_status"]["value"] == {"key": "yes", "input_value": ""}
    assert result["replaced"] == 1


def test_stronger_model_claim_is_kept():
    """模型若给出更硬的档（此处同为 fact 但注入也 fact），按同档规则注入胜；
    反向：模型值若为 `fact` 而已有注入…… 由上面覆盖。此处验证注入不吞掉别的字段。"""
    merged, _ = _inject(
        _records(),
        _claims(
            {"attribute": "phone_number", "value": "99999999999", "basis": "inferred_low"},
            {"attribute": "occupation", "value": "教师", "basis": "fact"},
        ),
    )
    assert merged["phone_number"]["value"] == "13800000000"
    assert merged["occupation"]["value"] == "教师"


def test_preserves_unrelated_claims_and_order():
    """注入不改动其它断言，也不打乱既有顺序。"""
    merged, _ = _inject(
        _records(),
        _claims(
            {"attribute": "occupation", "value": "教师", "basis": "fact"},
            {"attribute": "age", "value": "40-49岁", "basis": "inferred_high"},
        ),
    )
    assert list(merged) == ["occupation", "age", "phone_number"]


# ---------------------------------------------------------------- 接线

def test_project_consumes_injection_not_localize():
    """接线回归：``L2_PROJECT`` 的 ``claims_json`` 必须来自注入层，而非直连定位层。

    这是本改造的**接线的面**——函数写对了但接线漏了，注入值根本不进账本。
    """
    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    project = nodes[pipe.L2_PROJECT]
    sources = {b.target: tuple(b.source) for b in project.bindings}
    assert sources["claims_json"] == (pipe.L1_INJECT, "claims_json")

    inject = nodes[pipe.L1_INJECT]
    inject_sources = {b.target: tuple(b.source) for b in inject.bindings}
    assert inject_sources["claims_json"] == (pipe.L1_LOCALIZE, "claims_json")
    assert inject_sources["phone_number"] == (pipe.START, "phone_number")
    assert pipe.L1_INJECT in pipe.CODE_SPECS


def test_localize_still_consumes_merge():
    """定位层仍接合并层（不能被注入层架空——注入在定位之后）。"""
    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    localize = nodes[pipe.L1_LOCALIZE]
    sources = {b.target: tuple(b.source) for b in localize.bindings}
    assert sources["claims_json"] == (pipe.L1_MERGE, "claims_json")
