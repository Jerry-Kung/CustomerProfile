from __future__ import annotations

import pytest

from src.config.settings import DifyConfig
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.dify.errors import DifyRequestFailed, DifyTimeout


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def iter_lines(self):
        for line in self._lines:
            yield line

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self):
        return {}


class _FakeGetResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakePostResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeStreamContext:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeHttpxClient:
    def __init__(self, *args, **kwargs):
        self.poll_bodies = kwargs.pop("poll_bodies", [])
        self.stream_lines = kwargs.pop("stream_lines", [])
        self.post_bodies = kwargs.pop("post_bodies", [])
        self.captured_headers: list[dict] = []
        self.captured_stream_payloads: list[dict] = []
        self.post_calls = 0
        self.get_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, json: dict, headers: dict, timeout=None):
        assert method == "POST"
        assert json["response_mode"] == "streaming"
        assert "phone_number" in json["inputs"]
        self.captured_headers.append(dict(headers))
        self.captured_stream_payloads.append(dict(json))
        return _FakeStreamContext(_FakeStreamResponse(self.stream_lines))

    def get(self, url: str, headers: dict, timeout=None):
        body = self.poll_bodies[min(self.get_calls, len(self.poll_bodies) - 1)]
        self.get_calls += 1
        return _FakeGetResponse(body)

    def post(self, url: str, json: dict, headers: dict, timeout=None):
        self.captured_headers.append(dict(headers))
        if self.post_bodies:
            body = self.post_bodies[min(self.post_calls, len(self.post_bodies) - 1)]
            self.post_calls += 1
            return _FakePostResponse(body)
        return _FakePostResponse({})


def _config() -> DifyConfig:
    return DifyConfig(
        base_url="http://118.145.238.55",
        run_url="http://118.145.238.55/v1/workflows/run",
        api_key_test="app-CP-TEST-KEY",
        api_key_production="app-CP-PROD-KEY",
        invitation_strategy_api_key="app-INV-KEY",
        user_id="klh",
        image_mode="file_id",
        stream_init_timeout_ms=100,
        poll_interval_ms=1,
        poll_timeout_ms=50,
    )


def test_streaming_then_polling_succeeded(monkeypatch):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{"workflow_run_id":"run-1"}}'],
            poll_bodies=[{"status": "running"}, {"status": "succeeded", "output": '{"card_id":"c1","nickname":"n1"}'}],
        )
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    result = client.run_workflow(customer_phonenumber="13800138000")
    assert result == '{"card_id":"c1","nickname":"n1"}'
    assert holder["client"].captured_headers[0]["Authorization"] == "Bearer app-CP-TEST-KEY"


def test_production_work_env_uses_production_key(monkeypatch):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{"workflow_run_id":"run-1"}}'],
            poll_bodies=[{"status": "succeeded", "output": '{"card_id":"c1","nickname":"n1"}'}],
        )
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    client.run_workflow(customer_phonenumber="13800138000", work_env="production")
    assert holder["client"].captured_headers[0]["Authorization"] == "Bearer app-CP-PROD-KEY"


def test_workflow_inputs_include_work_env_and_batch_id(monkeypatch):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{"workflow_run_id":"run-1"}}'],
            poll_bodies=[{"status": "succeeded", "output": '{"card_id":"c1","nickname":"n1"}'}],
        )
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    client.run_workflow(
        customer_phonenumber="13800138000",
        work_env="production",
        batch_id="batch-2026Q2",
        task_type="invitation_strategy",
    )
    inputs = holder["client"].captured_stream_payloads[0]["inputs"]
    assert inputs == {
        "phone_number": "13800138000",
        "work_env": "production",
        "batch_id": "batch-2026Q2",
    }


def test_invitation_strategy_uses_dedicated_key_regardless_of_work_env(monkeypatch):
    holder: dict = {}

    def _factory(*args, **kwargs):
        client = _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{"workflow_run_id":"run-1"}}'],
            poll_bodies=[{"status": "succeeded", "output": '{"card_id":"c1","nickname":"n1"}'}],
        )
        holder["client"] = client
        return client

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    client.run_workflow(
        customer_phonenumber="13800138000",
        work_env="production",
        task_type="invitation_strategy",
    )
    assert holder["client"].captured_headers[0]["Authorization"] == "Bearer app-INV-KEY"


def test_streaming_missing_run_id(monkeypatch):
    def _factory(*args, **kwargs):
        return _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{}}'],
            poll_bodies=[{"status": "succeeded"}],
        )

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    with pytest.raises(DifyRequestFailed):
        client.run_workflow(customer_phonenumber="13800138000")


def test_polling_timeout(monkeypatch):
    def _factory(*args, **kwargs):
        return _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{"workflow_run_id":"run-1"}}'],
            poll_bodies=[{"status": "running"}],
        )

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    with pytest.raises(DifyTimeout):
        client.run_workflow(customer_phonenumber="13800138000")


def test_fallback_to_blocking_when_stream_has_no_run_id(monkeypatch):
    def _factory(*args, **kwargs):
        return _FakeHttpxClient(
            *args,
            stream_lines=['data: {"event":"workflow_started","data":{}}'],
            post_bodies=[{"data": {"workflow_run_id": "run-fallback"}}],
            poll_bodies=[{"status": "succeeded", "output": '{"card_id":"c1","nickname":"n1"}'}],
        )

    monkeypatch.setattr("src.infrastructure.dify.client.httpx.Client", _factory)
    client = DifyClient(config=_config())
    result = client.run_workflow(customer_phonenumber="13800138000")
    assert result == '{"card_id":"c1","nickname":"n1"}'
