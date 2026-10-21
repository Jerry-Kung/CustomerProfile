from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_TASK_TYPES: set[str] = {"customer_profile", "invitation_strategy"}
DEFAULT_TASK_TYPE = "customer_profile"


class SubmitTaskPhoneForm(BaseModel):
    """Validates `customer_phonenumber` for multipart `POST /v1/customer-profile/tasks`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    customer_phonenumber: str = Field(min_length=1, max_length=32)

    @field_validator("customer_phonenumber")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not all(ch.isdigit() or ch == "+" for ch in v):
            raise ValueError("customer_phonenumber format is invalid")
        return v


class SubmitTaskBatchIdForm(BaseModel):
    """Validates optional `batch_id` form field for task submission."""

    model_config = ConfigDict(str_strip_whitespace=True)

    batch_id: str | None = Field(default=None, max_length=64)

    @field_validator("batch_id", mode="before")
    @classmethod
    def empty_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id_chars(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not all(ch.isalnum() or ch in "-_" for ch in v):
            raise ValueError("batch_id contains invalid characters")
        return v


class SubmitTaskWorkEnvForm(BaseModel):
    """Validates optional `work_env` form field for task submission."""

    model_config = ConfigDict(str_strip_whitespace=True)

    work_env: str = Field(default="test")

    @field_validator("work_env", mode="before")
    @classmethod
    def normalize_work_env(cls, v: str | None) -> str:
        if v is None:
            return "test"
        value = str(v).strip().lower()
        if value == "develop":
            return "test"
        return value or "test"

    @field_validator("work_env")
    @classmethod
    def validate_work_env(cls, v: str) -> str:
        if v not in {"test", "production"}:
            raise ValueError("work_env must be one of: test, production")
        return v


class SubmitTaskTypeForm(BaseModel):
    """Validates optional `task_type` form/query field for task submission and lookup."""

    model_config = ConfigDict(str_strip_whitespace=True)

    task_type: str = Field(default=DEFAULT_TASK_TYPE)

    @field_validator("task_type", mode="before")
    @classmethod
    def normalize_task_type(cls, v: str | None) -> str:
        if v is None:
            return DEFAULT_TASK_TYPE
        value = str(v).strip().lower()
        return value or DEFAULT_TASK_TYPE

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, v: str) -> str:
        if v not in ALLOWED_TASK_TYPES:
            allowed = ", ".join(sorted(ALLOWED_TASK_TYPES))
            raise ValueError(f"task_type must be one of: {allowed}")
        return v


class SubmitTaskExecutionIdForm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    execution_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @field_validator("execution_id", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        if value is None:
            return None
        return str(value).strip() or None


class TaskIdentityResponse(BaseModel):
    task_id: int | None = None
    generation: int = 1
    batch_id: str | None = None
    execution_id: str | None = None


class TaskAcceptedResponse(TaskIdentityResponse):
    code: str
    message: str
    customer_phonenumber: str
    status: str
    task_type: str = DEFAULT_TASK_TYPE


class TaskListQuery(BaseModel):
    phone_prefix: str = Field(min_length=1, max_length=32)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    task_type: str | None = None

    @field_validator("phone_prefix")
    @classmethod
    def validate_phone_prefix(cls, v: str) -> str:
        if not all(ch.isdigit() or ch == "+" for ch in v):
            raise ValueError("phone_prefix format is invalid")
        return v

    @field_validator("task_type", mode="before")
    @classmethod
    def normalize_task_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip().lower()
        return value or None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in ALLOWED_TASK_TYPES:
            allowed = ", ".join(sorted(ALLOWED_TASK_TYPES))
            raise ValueError(f"task_type must be one of: {allowed}")
        return v


class TaskListItem(BaseModel):
    customer_phonenumber: str
    card_id: str | None = None
    nickname: str | None = None
    status: str
    updated_at: str | None = None
    task_type: str = DEFAULT_TASK_TYPE


class TaskListResponse(BaseModel):
    code: str
    message: str
    items: list[TaskListItem]
    page: int
    page_size: int
    total: int


class TaskResultSucceededResponse(TaskIdentityResponse):
    code: str
    message: str
    customer_phonenumber: str
    status: str
    output: dict
    card_id: str | None = None
    nickname: str | None = None
    updated_at: str | None = None
    task_type: str = DEFAULT_TASK_TYPE


class TaskResultInProgressResponse(TaskIdentityResponse):
    code: str
    message: str
    customer_phonenumber: str
    status: str
    updated_at: str | None = None
    task_type: str = DEFAULT_TASK_TYPE


class TaskResultFailedResponse(TaskIdentityResponse):
    last_error_code: str | None = None
    last_error_stage: str | None = None
    code: str
    message: str
    customer_phonenumber: str
    status: str
    error_message: str | None = None
    updated_at: str | None = None
    task_type: str = DEFAULT_TASK_TYPE
