from __future__ import annotations

import json

import pytest

from src.config.settings import QingTianConfig
from src.infrastructure.qingtian.client import QUERY_PATH, RUN_PATH, QingTianClient
from src.infrastructure.qingtian.errors import (
    QingTianOutputInvalidJson,
    QingTianRequestFailed,
    QingTianTimeout,
)


class _PostCall:
    def __init__(self, url: str, json_body: dict, headers: dict):
        self.url = url
        self.json_body = json_body
        self.headers = headers


class _FakePostResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeHttpxClient:
    def __init__(self, *args, post_bodies: list[dict] | None = None, **kwargs):
        # post_bodies feeds responses in order; the last one repeats indefinitely.
        self.post_bodies = post_bodies or []
        self.calls: list[_PostCall] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, json: dict, headers: dict, timeout=None):
        self.calls.append(_PostCall(url=url, json_body=dict(json), headers=dict(headers)))
        body = self.post_bodies[min(len(self.calls) - 1, len(self.post_bodies) - 1)]
        return _FakePostResponse(body)


def _config() -> QingTianConfig:
    return QingTianConfig(
        base_url="https://qingtian.example.com/api/proxy/api/v1",
        api_key_test="qt-TEST-KEY",
        api_key_production="qt-PROD-KEY",
        invitation_strategy_api_key="qt-INV-KEY",
        user_id="klh",
        poll_interval_ms=1,
        poll_timeout_ms=50,
    )


def _install(monkeypatch, **factory_kwargs):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(*args, **factory_kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.qingtian.client.httpx.Client", _factory)
    return holder


def test_happy_path_submit_then_poll_success(monkeypatch):
    holder = _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-123"},
            {"status": "processing"},
            {"status": "success", "output": '{"card_id":"c1","nickname":"n1"}'},
        ],
    )
    client = QingTianClient(config=_config())
    result = client.run_workflow(customer_phonenumber="13800138000")
    assert result == '{"card_id":"c1","nickname":"n1"}'

    calls = holder["client"].calls
    assert calls[0].url.endswith(RUN_PATH)
    assert all(c.url.endswith(QUERY_PATH) for c in calls[1:])
    # Headers carry Apikey, not Bearer auth.
    assert calls[0].headers["Apikey"] == "qt-TEST-KEY"
    # Body wires AppKey == Apikey.
    assert calls[0].json_body["AppKey"] == "qt-TEST-KEY"
    # InputData must be a JSON string (not an object).
    input_data_raw = calls[0].json_body["InputData"]
    assert isinstance(input_data_raw, str)
    assert json.loads(input_data_raw) == {
        "phone_number": "13800138000",
        "work_env": "test",
        "batch_id": "",
    }
    # Query body shape.
    assert calls[1].json_body == {"RunID": "run-123", "UserID": "klh", "AppKey": "qt-TEST-KEY"}


def test_production_work_env_uses_production_key(monkeypatch):
    holder = _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "success", "output": '{"card_id":"c","nickname":"n"}'},
        ],
    )
    client = QingTianClient(config=_config())
    client.run_workflow(customer_phonenumber="13800138000", work_env="production")
    assert holder["client"].calls[0].headers["Apikey"] == "qt-PROD-KEY"
    assert holder["client"].calls[0].json_body["AppKey"] == "qt-PROD-KEY"


def test_invitation_strategy_uses_dedicated_key_regardless_of_work_env(monkeypatch):
    holder = _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "success", "output": '{"card_id":"c","nickname":"n"}'},
        ],
    )
    client = QingTianClient(config=_config())
    client.run_workflow(
        customer_phonenumber="13800138000",
        work_env="production",
        task_type="invitation_strategy",
    )
    assert holder["client"].calls[0].headers["Apikey"] == "qt-INV-KEY"


def test_workflow_inputs_include_batch_id(monkeypatch):
    holder = _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "success", "output": '{"card_id":"c","nickname":"n"}'},
        ],
    )
    client = QingTianClient(config=_config())
    client.run_workflow(
        customer_phonenumber="13800138000",
        work_env="production",
        batch_id="batch-2026Q2",
    )
    inputs = json.loads(holder["client"].calls[0].json_body["InputData"])
    assert inputs == {
        "phone_number": "13800138000",
        "work_env": "production",
        "batch_id": "batch-2026Q2",
    }


def test_missing_run_id_raises_request_failed(monkeypatch):
    _install(monkeypatch, post_bodies=[{}])
    client = QingTianClient(config=_config())
    with pytest.raises(QingTianRequestFailed):
        client.run_workflow(customer_phonenumber="13800138000")


def test_polling_timeout(monkeypatch):
    _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "processing"},
        ],
    )
    client = QingTianClient(config=_config())
    with pytest.raises(QingTianTimeout):
        client.run_workflow(customer_phonenumber="13800138000")


@pytest.mark.parametrize("status", ["failed", "stopped", "interrupted", "timeout"])
def test_failure_statuses_raise_request_failed(monkeypatch, status):
    _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": status, "message": "boom", "code": 500},
        ],
    )
    client = QingTianClient(config=_config())
    with pytest.raises(QingTianRequestFailed) as exc:
        client.run_workflow(customer_phonenumber="13800138000")
    assert status in str(exc.value)


def test_success_without_output_raises_invalid_json(monkeypatch):
    _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "success", "output": ""},
        ],
    )
    client = QingTianClient(config=_config())
    with pytest.raises(QingTianOutputInvalidJson):
        client.run_workflow(customer_phonenumber="13800138000")


def test_unknown_status_keeps_polling_until_terminal(monkeypatch):
    _install(
        monkeypatch,
        post_bodies=[
            {"runId": "run-1"},
            {"status": "unexpected_state"},
            {"status": "success", "output": '{"card_id":"c","nickname":"n"}'},
        ],
    )
    client = QingTianClient(config=_config())
    # Should not raise — unknown statuses are treated as non-terminal.
    result = client.run_workflow(customer_phonenumber="13800138000")
    assert result == '{"card_id":"c","nickname":"n"}'
