from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import create_api_app


class _TaskService:
    def submit_task(self, customer_phonenumber: str, **_kwargs):
        return None

    def claim_next_task(self):
        return None


class _UploadStore:
    pass


class _DBOK:
    def ping(self) -> bool:
        return True


class _DBDown:
    def ping(self) -> bool:
        return False


def test_health_check_up():
    app = create_api_app(db_client=_DBOK(), task_service=_TaskService(), upload_store=_UploadStore())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db_status": "up"}


def test_health_check_down():
    app = create_api_app(db_client=_DBDown(), task_service=_TaskService(), upload_store=_UploadStore())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db_status": "down"}

