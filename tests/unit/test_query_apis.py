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
    def submit_task(self, customer_phonenumber: str, **_kwargs):
        return TaskRecord(id=1, customer_phonenumber=customer_phonenumber, status="queued")

    def claim_next_task(self):
        return None

    def list_tasks(self, *, phone_prefix: str, page: int, page_size: int, task_type: str | None = None):
        items = [
            TaskRecord(
                id=1,
                customer_phonenumber=f"{phone_prefix}001",
                status="succeeded",
                card_id="c1",
                nickname="n1",
                updated_at="2026-03-23T12:00:00",
                output={"card_id": "c1", "nickname": "n1"},
                task_type="customer_profile",
            ),
            TaskRecord(
                id=2,
                customer_phonenumber=f"{phone_prefix}002",
                status="queued",
                updated_at="2026-03-23T12:05:00",
                task_type="invitation_strategy",
            ),
        ]
        if task_type is not None:
            items = [i for i in items if i.task_type == task_type]
        return items, len(items)

    def get_task_by_phone(self, customer_phonenumber: str, *, task_type: str = "customer_profile"):
        mapping = {
            ("13800000001", "customer_profile"): TaskRecord(
                id=1,
                customer_phonenumber="13800000001",
                status="succeeded",
                output={"card_id": "c1", "nickname": "n1"},
                card_id="c1",
                nickname="n1",
                updated_at="2026-03-23T12:00:00",
                task_type="customer_profile",
            ),
            ("13800000002", "customer_profile"): TaskRecord(
                id=2,
                customer_phonenumber="13800000002",
                status="processing",
                updated_at="2026-03-23T12:01:00",
                task_type="customer_profile",
            ),
            ("13800000003", "customer_profile"): TaskRecord(
                id=3,
                customer_phonenumber="13800000003",
                status="failed",
                error_message="DIFY_TIMEOUT: polling timeout",
                updated_at="2026-03-23T12:02:00",
                task_type="customer_profile",
            ),
            ("13800000001", "invitation_strategy"): TaskRecord(
                id=4,
                customer_phonenumber="13800000001",
                status="succeeded",
                output={"card_id": "inv-1", "nickname": "inv-nick"},
                card_id="inv-1",
                nickname="inv-nick",
                updated_at="2026-03-23T12:10:00",
                task_type="invitation_strategy",
            ),
        }
        return mapping.get((customer_phonenumber, task_type))


def _client() -> TestClient:
    app = create_api_app(db_client=_DBOK(), task_service=_TaskService(), upload_store=_UploadStore())
    return TestClient(app)


def test_list_tasks_success():
    client = _client()
    response = client.get("/v1/customer-profile/tasks", params={"phone_prefix": "138", "page": 1, "page_size": 20})
    body = response.json()
    assert response.status_code == 200
    assert body["code"] == "OK"
    assert body["total"] == 2
    assert "output" not in body["items"][0]
    assert {item["task_type"] for item in body["items"]} == {"customer_profile", "invitation_strategy"}


def test_list_tasks_filtered_by_task_type():
    client = _client()
    response = client.get(
        "/v1/customer-profile/tasks",
        params={"phone_prefix": "138", "task_type": "invitation_strategy"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert body["items"][0]["task_type"] == "invitation_strategy"


def test_list_tasks_invalid_prefix():
    client = _client()
    response = client.get("/v1/customer-profile/tasks", params={"phone_prefix": "abc", "page": 1, "page_size": 20})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_list_tasks_invalid_task_type():
    client = _client()
    response = client.get(
        "/v1/customer-profile/tasks",
        params={"phone_prefix": "138", "task_type": "no_such"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


def test_get_result_succeeded():
    client = _client()
    response = client.get("/v1/customer-profile/tasks/13800000001/result")
    body = response.json()
    assert response.status_code == 200
    assert body["code"] == "TASK_SUCCEEDED"
    assert body["status"] == "succeeded"
    assert body["output"]["card_id"] == "c1"
    assert body["task_type"] == "customer_profile"


def test_get_result_succeeded_invitation_strategy():
    client = _client()
    response = client.get(
        "/v1/customer-profile/tasks/13800000001/result",
        params={"task_type": "invitation_strategy"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["task_type"] == "invitation_strategy"
    assert body["output"]["card_id"] == "inv-1"


def test_get_result_in_progress():
    client = _client()
    response = client.get("/v1/customer-profile/tasks/13800000002/result")
    body = response.json()
    assert response.status_code == 202
    assert body["code"] == "TASK_PROCESSING"
    assert body["status"] == "processing"
    assert body["task_type"] == "customer_profile"


def test_get_result_failed():
    client = _client()
    response = client.get("/v1/customer-profile/tasks/13800000003/result")
    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "DIFY_TIMEOUT"
    assert body["status"] == "failed"
    assert body["task_type"] == "customer_profile"


def test_get_result_not_found():
    client = _client()
    response = client.get("/v1/customer-profile/tasks/13899999999/result")
    body = response.json()
    assert response.status_code == 404
    assert body["code"] == "TASK_NOT_FOUND"


def test_get_result_invalid_task_type_returns_400():
    client = _client()
    response = client.get(
        "/v1/customer-profile/tasks/13800000001/result",
        params={"task_type": "no_such"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"
