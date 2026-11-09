"""
HTTP API integration tests against a **running** service, using `curl` subprocess.

Prerequisites:
  - API reachable at API_E2E_BASE_URL (default http://127.0.0.1:8000)
  - `curl` on PATH
  - Set RUN_API_HTTP_E2E=1

Test data: tests/test_data (phone + image paths as local files for Worker on same machine).

## 用例与接口对应（提交任务已覆盖）

| 测试函数 | 说明 |
|----------|------|
| test_health_ok | GET /health |
| test_submit_task_invalid_phone | **POST 提交任务**，非法手机号 → 400 |
| test_submit_task_phone_only | **POST 提交任务**，仅手机号（无图）→ 202 |
| test_submit_task_accepts_test_data | **POST 提交任务**，tests/test_data 手机号 + 两张本地图片路径 → 202 |
| test_list_tasks_by_prefix | GET /v1/customer-profile/tasks |
| test_get_result_in_progress_or_terminal | GET .../result（依赖上文提交的同号） |
| test_get_result_not_found | GET .../result 不存在号码 → 404 |
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.getenv("RUN_API_HTTP_E2E") != "1",
        reason="set RUN_API_HTTP_E2E=1 and start the API to run HTTP curl e2e tests",
    ),
    pytest.mark.skipif(not shutil.which("curl"), reason="curl not found on PATH"),
    pytest.mark.integration,
    pytest.mark.api_http_e2e,
]

ROOT = Path(__file__).resolve().parents[2]
TEST_DATA = ROOT / "tests" / "test_data"


def _base_url() -> str:
    return os.getenv("API_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _curl(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
) -> tuple[int, dict | list | str]:
    """Run curl; return (status_code, parsed_json_or_raw_text)."""
    body_file = tempfile.NamedTemporaryFile(delete=False)
    body_path = body_file.name
    body_file.close()
    payload_path: str | None = None
    try:
        cmd = ["curl", "-sS", "-X", method, url]
        if json_body is not None:
            payload_path = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                delete=False,
            ).name
            Path(payload_path).write_text(json.dumps(json_body, ensure_ascii=False), encoding="utf-8")
            cmd.extend(["-H", "Content-Type: application/json", "-d", f"@{payload_path}"])
        cmd.extend(["-o", body_path, "-w", "%{http_code}"])

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.stderr:
            # curl -sS still prints errors to stderr
            raise AssertionError(f"curl stderr: {result.stderr.strip()}")
        status = int(result.stdout.strip())
        raw = Path(body_path).read_text(encoding="utf-8")
        try:
            parsed: dict | list | str = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = raw
        return status, parsed
    finally:
        Path(body_path).unlink(missing_ok=True)
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)


def _load_phone_normalized() -> str:
    raw = (TEST_DATA / "customer_phonenumber.txt").read_text(encoding="utf-8").strip()
    return raw.replace(" ", "")


@pytest.fixture(scope="module")
def phone() -> str:
    p = _load_phone_normalized()
    if not p or not all(c.isdigit() or c == "+" for c in p):
        pytest.skip("tests/test_data/customer_phonenumber.txt must yield API-valid phone (digits/+)")
    return p


def test_health_ok():
    status, body = _curl("GET", f"{_base_url()}/health")
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("status") == "ok"
    assert body.get("db_status") in {"up", "down"}


def test_submit_task_invalid_phone():
    """POST /v1/customer-profile/tasks：非法 customer_phonenumber → 400 INVALID_ARGUMENT。"""
    status, body = _curl(
        "POST",
        f"{_base_url()}/v1/customer-profile/tasks",
        json_body={"customer_phonenumber": "not-a-phone"},
    )
    assert status == 400
    assert isinstance(body, dict)
    assert body.get("code") == "INVALID_ARGUMENT"


def test_submit_task_phone_only(phone: str):
    """POST /v1/customer-profile/tasks：仅必填手机号（图片可选），期望 202 TASK_ACCEPTED。"""
    status, body = _curl(
        "POST",
        f"{_base_url()}/v1/customer-profile/tasks",
        json_body={"customer_phonenumber": phone},
    )
    assert status == 202
    assert isinstance(body, dict)
    assert body.get("code") == "TASK_ACCEPTED"
    assert body.get("customer_phonenumber") == phone
    assert body.get("status") == "queued"


def test_submit_task_accepts_test_data(phone: str):
    """POST /v1/customer-profile/tasks：使用 tests/test_data 中手机号与两张截图路径提交，期望 202。"""
    wechat = (TEST_DATA / "customer_wechat_homepage.jpg").resolve()
    moment = (TEST_DATA / "customer_moment_homepage.jpg").resolve()
    if not wechat.is_file() or not moment.is_file():
        pytest.skip("test images missing under tests/test_data")

    status, body = _curl(
        "POST",
        f"{_base_url()}/v1/customer-profile/tasks",
        json_body={
            "customer_phonenumber": phone,
            "wechat_homepage_image": wechat.as_posix(),
            "wechat_moments_image": moment.as_posix(),
        },
    )
    assert status == 202
    assert isinstance(body, dict)
    assert body.get("code") == "TASK_ACCEPTED"
    assert body.get("customer_phonenumber") == phone
    assert body.get("status") == "queued"


def test_list_tasks_by_prefix(phone: str):
    prefix = phone[:3] if len(phone) >= 3 else phone
    status, body = _curl(
        "GET",
        f"{_base_url()}/v1/customer-profile/tasks?phone_prefix={prefix}&page=1&page_size=10",
    )
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("code") == "OK"
    assert "items" in body
    assert isinstance(body["items"], list)


def test_get_result_in_progress_or_terminal(phone: str):
    from urllib.parse import quote

    path_phone = quote(phone, safe="")
    status, body = _curl("GET", f"{_base_url()}/v1/customer-profile/tasks/{path_phone}/result")
    assert status in {200, 202, 422}
    assert isinstance(body, dict)
    if status == 202:
        assert body.get("code") in {"TASK_QUEUED", "TASK_PROCESSING"}
        assert body.get("customer_phonenumber") == phone
    elif status == 200:
        assert body.get("code") == "TASK_SUCCEEDED"
        assert body.get("status") == "succeeded"
    else:
        assert body.get("status") == "failed"


def test_get_result_not_found():
    from urllib.parse import quote

    fake = "19999999999"
    status, body = _curl("GET", f"{_base_url()}/v1/customer-profile/tasks/{quote(fake, safe='')}/result")
    assert status == 404
    assert isinstance(body, dict)
    assert body.get("code") == "TASK_NOT_FOUND"
