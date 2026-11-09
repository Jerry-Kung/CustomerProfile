from __future__ import annotations

from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_submit_preserves_form_identity_and_explicit_environment(monkeypatch, capsys):
    submit = load_script("submit_task")
    captured = {}

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"queued"}'

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["form"] = parse_qs(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(submit.urllib.request, "urlopen", open_request)
    assert submit.main([
        "--phone", "13800138000", "--batch-id", "batch + 中文",
        "--base-url", "http://backend:18008/", "--work-env", "production",
        "--task-type", "invitation_strategy", "--timeout", "7",
    ]) == 0
    assert captured == {
        "url": "http://backend:18008/v1/customer-profile/tasks",
        "timeout": 7.0,
        "form": {
            "customer_phonenumber": ["13800138000"],
            "batch_id": ["batch + 中文"],
            "work_env": ["production"],
            "task_type": ["invitation_strategy"],
        },
    }
    assert "202" in capsys.readouterr().out


def test_submit_returns_failure_for_an_http_rejection(monkeypatch, capsys):
    submit = load_script("submit_task")

    def reject_request(request, timeout):
        raise HTTPError(request.full_url, 429, "queue full", {}, io.BytesIO(b'{"code":"TASK_QUEUE_FULL"}'))

    monkeypatch.setattr(submit.urllib.request, "urlopen", reject_request)
    assert submit.main(["--phone", "13800138000", "--batch-id", "test"]) == 1
    assert "TASK_QUEUE_FULL" in capsys.readouterr().out


def test_inspection_query_keeps_values_out_of_sql():
    inspect = load_script("inspect_phone_tasks")
    phone = "138' OR 1=1 --"
    sql, params = inspect.build_query(phone, [123, 456], 20)
    assert sql.startswith("SELECT ")
    assert phone not in sql
    assert "customer_phonenumber = %s OR id IN (%s, %s)" in sql
    assert params == (phone, 123, 456, 20)


def test_inspection_uses_shared_config_and_closes_resources(monkeypatch, capsys):
    inspect = load_script("inspect_phone_tasks")
    mysql_config = object()
    captured = {}

    class Cursor:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"id": 123, "status": "queued", "updated_at": datetime(2026, 1, 1)}]

        def close(self):
            captured["cursor_closed"] = True

    class Connection:
        def cursor(self, *, dictionary):
            assert dictionary
            return Cursor()

        def close(self):
            captured["connection_closed"] = True

    def configuration():
        return mysql_config

    def client(config):
        assert config is mysql_config
        return SimpleNamespace(connect=lambda: Connection())

    monkeypatch.setattr(inspect, "load_mysql_config", configuration)
    monkeypatch.setattr(inspect, "MySQLClient", client)
    assert inspect.main(["--task-id", "123"]) == 0
    assert captured["params"] == (123, 100)
    assert captured["cursor_closed"] and captured["connection_closed"]
    assert json.loads(capsys.readouterr().out)[0]["updated_at"] == "2026-01-01 00:00:00"


@pytest.mark.parametrize("arguments", [[], ["--task-id", "-1"], ["--task-id", "1", "--limit", "1001"]])
def test_inspection_rejects_unbounded_or_invalid_queries_before_loading_config(monkeypatch, arguments):
    inspect = load_script("inspect_phone_tasks")

    def unexpected_config(**kwargs):
        pytest.fail("invalid arguments must not load configuration or open a database")

    monkeypatch.setattr(inspect, "load_mysql_config", unexpected_config)
    with pytest.raises(SystemExit) as error:
        inspect.main(arguments)
    assert error.value.code == 2


def test_database_tool_config_does_not_require_workflow_credentials(monkeypatch):
    import src.config.settings as settings

    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: None)
    for key, value in {
        "MYSQL_HOST": "database",
        "MYSQL_USER": "operator",
        "MYSQL_PASSWORD": "test-only",
        "MYSQL_DATABASE": "customer_profile",
        "MYSQL_PORT": "3307",
        "WORKFLOW_PLATFORM": "runner",
        "WORKFLOW_TARGET_ID": "",
        "PROFILE_RUNNER_BASE_URL_PRODUCTION": "",
        "PROFILE_RUNNER_BASE_URL_TEST": "",
    }.items():
        monkeypatch.setenv(key, value)
    config = settings.load_mysql_config()
    assert (config.host, config.port, config.user, config.database) == (
        "database", 3307, "operator", "customer_profile",
    )
