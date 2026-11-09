from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.app import create_api_app


class _TaskService:
    def submit_task(self, customer_phonenumber: str, **_kwargs):
        return None

    def claim_next_task(self):
        return None


class _DBOK:
    def ping(self) -> bool:
        return True


class _DBDown:
    def ping(self) -> bool:
        return False


def test_health_check_up():
    app = create_api_app(db_client=_DBOK(), task_service=_TaskService())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("x-request-id")
    assert response.json() == {"status": "ok", "db_status": "up"}


def test_health_check_down():
    app = create_api_app(db_client=_DBDown(), task_service=_TaskService())
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get("x-request-id")
    assert response.json() == {"status": "ok", "db_status": "down"}



def test_readiness_reports_database_failure_with_http_503():
    app = create_api_app(db_client=_DBDown(), task_service=_TaskService())
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "db_status": "down"}


def test_readiness_up():
    app = create_api_app(db_client=_DBOK(), task_service=_TaskService())
    assert TestClient(app).get("/ready").status_code == 200
