"""邀约的输入与模型输出契约；业务身份由代码生成，不交给模型。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
PHONE_TITLES = ("开场", "需求确认", "邀约切入", "时间确认", "收尾")
AUDIT_ITEMS = {
    "process": ("流程执行", ("开场自我介绍", "需求探询", "邀约闭环")),
    "expression": ("沟通表达", ("语速与语气", "倾听与回应", "话术自然度")),
    "knowledge": ("专业知识", ("产品卖点准确性", "竞品与政策")),
    "invitation": ("邀约引导", ("邀约理由", "时间地点确认", "异议处理")),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InvitationInputs(Contract):
    phone_number: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^1[3-9][0-9]{9}$")
    ]
    batch_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ]
    work_env: Literal["test", "production"] = "test"
    execution_id: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None
    ) = None


class Reasoning(Contract):
    summary: Text
    steps: list[Text] = Field(min_length=2, max_length=4)


class Strategy(Contract):
    recommended_channel: Literal[
        "微信先行",
        "电话先行",
        "微信铺垫后电话确认",
        "电话沟通后微信跟进",
        "低频微信跟进",
    ]
    communication_stance: list[Text] = Field(min_length=1)
    key_entry_points: list[Text] = Field(min_length=1)
    avoid_words: list[Text] = Field(min_length=1)
    reasoning: Reasoning


class PhoneStep(Contract):
    title: Text
    content: Text


class Script(Contract):
    strategy: Strategy
    phone_steps: list[PhoneStep] = Field(min_length=5, max_length=5)
    wechat_script: Text

    @model_validator(mode="after")
    def fixed_steps(self):
        if tuple(step.title for step in self.phone_steps) != PHONE_TITLES:
            raise ValueError("电话话术须包含五个固定步骤，并按约定顺序输出")
        return self


class ScriptOutput(Contract):
    script: Script


class AuditItem(Contract):
    order: Annotated[int, Field(strict=True, ge=1)]
    name: Text
    result: Literal["pass", "partial", "fail"]
    highlights: list[Text]
    issues: list[Text]


class AuditDimension(Contract):
    key: str
    name: Text
    score: Score
    items: list[AuditItem]


class Audit(Contract):
    overall_score: Score
    overall_rating: str
    rating_description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    dimensions: list[AuditDimension] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def fixed_dimensions(self):
        if tuple(d.key for d in self.dimensions) != tuple(AUDIT_ITEMS):
            raise ValueError("质检须包含四个固定维度，并按约定顺序输出")
        for dim in self.dimensions:
            name, items = AUDIT_ITEMS[dim.key]
            if dim.name != name or tuple(i.name for i in dim.items) != items:
                raise ValueError(f"质检维度 {dim.key} 的名称或检核项不符合约定")
            if [i.order for i in dim.items] != list(range(1, len(items) + 1)):
                raise ValueError("检核项 order 须从 1 连续递增")
        # 四舍五入，避免 Python round 在 x.5 时采用偶数舍入。
        self.overall_score = (sum(d.score for d in self.dimensions) + 2) // 4
        self.overall_rating = next(
            label
            for floor, label in (
                (90, "优秀"),
                (80, "良好"),
                (70, "合格"),
                (60, "待改进"),
                (0, "不合格"),
            )
            if self.overall_score >= floor
        )
        return self


class AuditOutput(Contract):
    audit: Audit


class FollowUpItem(Contract):
    order: Annotated[int, Field(strict=True, ge=1)]
    content: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ]


class FollowUpOutput(Contract):
    follow_up_summary: Text
    follow_up_items: list[FollowUpItem]

    @model_validator(mode="after")
    def ordered_items(self):
        if [i.order for i in self.follow_up_items] != list(
            range(1, len(self.follow_up_items) + 1)
        ):
            raise ValueError("待办 order 须从 1 连续递增")
        return self


class AudioValidity(Contract):
    is_valid: bool
    reason: str


def _validate(model: type[Contract], value: Any) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump()
    except ValidationError as exc:
        # 错误只带字段位置与规则，不把整份客户资料复制进重试错误。
        details = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['type']} ({e['msg']})"
            for e in exc.errors()
        )
        raise ValueError(details) from None


def validate_script(value: Any) -> dict[str, Any]:
    return _validate(ScriptOutput, value)


def validate_audit(value: Any) -> dict[str, Any]:
    return _validate(AuditOutput, value)


def validate_follow_up(value: Any) -> dict[str, Any]:
    return _validate(FollowUpOutput, value)


def validate_audio_validity(value: Any) -> dict[str, Any]:
    return _validate(AudioValidity, value)


def output_schema(validator: str) -> dict[str, Any]:
    """请求约束与本地校验复用同一份模型，避免各自维护字段结构。"""
    model = {
        "validate_script": ScriptOutput,
        "validate_audit": AuditOutput,
        "validate_follow_up": FollowUpOutput,
        "validate_audio_validity": AudioValidity,
    }[validator]
    return {"name": model.__name__, "schema": model.model_json_schema()}
