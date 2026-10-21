from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import create_api_app
from src.domain.task.models import TaskRecord


class _DBOK:
    def ping(self) -> bool:
        return True


class _UploadStore:
    pass


class _TaskService:
    def __init__(self):
        self.calls: list[dict] = []

    def submit_task(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = "customer_profile",
        execution_id: str | None = None,
    ) -> TaskRecord:
        self.calls.append(
            {
                "customer_phonenumber": customer_phonenumber,
                "work_env": work_env,
                "batch_id": batch_id,
                "task_type": task_type,
            }
        )
        if execution_id:
            self.calls[-1]["execution_id"] = execution_id
        return TaskRecord(
            id=1,
            customer_phonenumber=customer_phonenumber,
            status="queued",
            work_env=work_env,
            batch_id=batch_id,
            task_type=task_type,
            execution_id=execution_id,
        )

    def claim_next_task(self):
        return None


def _client(service: _TaskService) -> TestClient:
    app = create_api_app(db_client=_DBOK(), task_service=service, upload_store=_UploadStore())
    return TestClient(app)


def test_submit_task_returns_202_with_default_task_type():
    service = _TaskService()
    response = _client(service).post(
        "/v1/customer-profile/tasks",
        data={"customer_phonenumber": "13800138000"},
    )
    body = response.json()
    assert response.status_code == 202
    assert body["code"] == "TASK_ACCEPTED"
    assert body["customer_phonenumber"] == "13800138000"
    assert body["status"] == "queued"
    assert body["task_type"] == "customer_profile"
    assert service.calls == [
        {
            "customer_phonenumber": "13800138000",
            "work_env": "test",
            "batch_id": None,
            "task_type": "customer_profile",
        }
    ]


def test_submit_task_accepts_invitation_strategy():
    service = _TaskService()
    response = _client(service).post(
        "/v1/customer-profile/tasks",
        data={
            "customer_phonenumber": "13800138000",
            "task_type": "invitation_strategy",
            "work_env": "production",
            "batch_id": "2026Q2-001",
        },
    )
    body = response.json()
    assert response.status_code == 202
    assert body["task_type"] == "invitation_strategy"
    assert service.calls[0]["task_type"] == "invitation_strategy"
    assert service.calls[0]["work_env"] == "production"
    assert service.calls[0]["batch_id"] == "2026Q2-001"


def test_invitation_execution_identity_survives_admission():
    service = _TaskService()
    response = _client(service).post("/v1/customer-profile/tasks", data={
        "customer_phonenumber": "13800138000", "task_type": "invitation_strategy",
        "batch_id": "recording-batch", "execution_id": "new-execution",
    })
    assert response.status_code == 202
    assert response.json()["execution_id"] == "new-execution"
    assert response.json()["batch_id"] == "recording-batch"
    assert service.calls[0]["execution_id"] == "new-execution"


def test_profile_task_cannot_accidentally_enable_invitation_identity():
    response = _client(_TaskService()).post("/v1/customer-profile/tasks", data={
        "customer_phonenumber": "13800138000", "execution_id": "invitation-only",
    })
    assert response.status_code == 400


def test_identified_invitation_requires_a_callback_batch():
    service = _TaskService()
    response = _client(service).post("/v1/customer-profile/tasks", data={
        "customer_phonenumber": "13800138000", "task_type": "invitation_strategy", "execution_id": "exec-1",
    })
    assert response.status_code == 400
    assert not service.calls


def test_submit_task_invalid_task_type_returns_400():
    service = _TaskService()
    response = _client(service).post(
        "/v1/customer-profile/tasks",
        data={"customer_phonenumber": "13800138000", "task_type": "no_such_type"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_submit_task_invalid_phone_returns_400():
    service = _TaskService()
    response = _client(service).post(
        "/v1/customer-profile/tasks",
        data={"customer_phonenumber": "abc-138"},
    )
    body = response.json()
    assert response.status_code == 400
    assert body["code"] == "INVALID_ARGUMENT"
