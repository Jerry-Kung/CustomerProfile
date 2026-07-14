from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import time

import httpx


ROOT = Path("d:/Huawei/Projects/swt_dfms/customer_profile_backend")
ENV_FILE = ROOT / ".env.example"
TEST_DATA_DIR = ROOT / "tests" / "test_data"


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value


def _mask(value: str) -> str:
    if not value:
        return "***"
    return f"{value[:4]}*** (len={len(value)})"


def _pretty(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _read_test_inputs() -> tuple[str, Path, Path]:
    phone = (TEST_DATA_DIR / "customer_phonenumber.txt").read_text(encoding="utf-8").strip()
    wechat = TEST_DATA_DIR / "customer_wechat_homepage.jpg"
    moment = TEST_DATA_DIR / "customer_moment_homepage.jpg"
    if not wechat.exists() or not moment.exists():
        raise FileNotFoundError("test images not found in tests/test_data")
    if not phone:
        raise ValueError("customer_phonenumber.txt is empty")
    return phone, wechat, moment


def _upload_file(client: httpx.Client, base_url: str, headers: dict[str, str], user_id: str, file_path: Path) -> str:
    url = f"{base_url.rstrip('/')}/v1/files/upload"
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    print(f"\n[UPLOAD] POST {url} file={file_path.name} size={file_path.stat().st_size}")
    print(f"[UPLOAD] mime_type={mime_type}")
    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f, mime_type)}
        data = {"user": user_id}
        response = client.post(url, headers={"Authorization": headers["Authorization"]}, data=data, files=files)
    print(f"[UPLOAD] status={response.status_code}")
    text = response.text[:3000]
    print(f"[UPLOAD] body={text}")
    response.raise_for_status()
    body = response.json()
    file_id = body.get("id") if isinstance(body, dict) else None
    if not file_id and isinstance(body, dict) and isinstance(body.get("data"), dict):
        file_id = body["data"].get("id")
    if not file_id:
        raise RuntimeError("upload succeeded but no file id found")
    print(f"[UPLOAD] file_id={file_id}")
    return str(file_id)


def _extract_run_id_from_line(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    payload = line.split("data:", 1)[1].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    for key in ("workflow_run_id", "run_id", "task_id"):
        if isinstance(obj.get(key), str) and obj[key].strip():
            return obj[key]
    data = obj.get("data")
    if isinstance(data, dict):
        for key in ("workflow_run_id", "run_id", "task_id"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key]
    return None


def run_probe() -> int:
    _load_env_file(ENV_FILE)
    base_url = os.getenv("DIFY_BASE_URL", "").strip()
    run_url = os.getenv("DIFY_RUN_URL", "").strip() or f"{base_url.rstrip('/')}/v1/workflows/run"
    api_key = os.getenv("DIFY_API_KEY", "").strip()
    user_id = os.getenv("DIFY_USER_ID", "").strip()
    stream_init_timeout_sec = max(5, int(os.getenv("DIFY_STREAM_INIT_TIMEOUT_MS", "30000")) // 1000)
    poll_timeout_sec = max(60, int(os.getenv("DIFY_POLL_TIMEOUT_MS", "900000")) // 1000)

    if not base_url or not run_url or not api_key or not user_id:
        print("[ERROR] Missing required Dify envs in .env.example")
        return 2

    phone, wechat_img, moment_img = _read_test_inputs()

    print("[CONFIG]")
    print(f"base_url={base_url}")
    print(f"run_url={run_url}")
    print(f"api_key={_mask(api_key)}")
    print(f"user_id={user_id}")
    print(f"phone={phone}")
    print(f"wechat_img={wechat_img}")
    print(f"moment_img={moment_img}")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=20.0, read=20.0, write=30.0, pool=20.0)

    with httpx.Client(timeout=timeout) as client:
        # Step 1: upload files
        try:
            wechat_file_id = _upload_file(client, base_url, headers, user_id, wechat_img)
            moment_file_id = _upload_file(client, base_url, headers, user_id, moment_img)
        except Exception as exc:
            print(f"[ERROR] upload failed: {exc}")
            return 3

        # Step 2: start workflow with streaming
        payload = {
            "inputs": {
                "phone_number": phone,
                "wechat_homepage": {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": wechat_file_id,
                },
                "moments_homepage": {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": moment_file_id,
                },
            },
            "response_mode": "streaming",
            "user": user_id,
        }
        print(f"\n[RUN-STREAMING] POST {run_url}")
        print(f"[RUN-STREAMING] payload={_pretty(payload)}")

        workflow_run_id: str | None = None
        try:
            with client.stream("POST", run_url, json=payload, headers=headers) as resp:
                print(f"[RUN-STREAMING] status={resp.status_code}")
                print(f"[RUN-STREAMING] headers={dict(resp.headers)}")
                resp.raise_for_status()
                deadline = time.time() + stream_init_timeout_sec
                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8") if isinstance(line, bytes) else line
                    print(f"[RUN-STREAMING] line={text[:2000]}")
                    run_id = _extract_run_id_from_line(text)
                    if run_id:
                        workflow_run_id = run_id
                        print(f"[RUN-STREAMING] workflow_run_id={workflow_run_id}")
                        break
                    if time.time() > deadline:
                        print("[RUN-STREAMING] timeout waiting first event")
                        break
        except Exception as exc:
            print(f"[RUN-STREAMING] exception={type(exc).__name__}: {exc}")

        # Step 3: if no run id from streaming, try blocking for diagnosis
        if not workflow_run_id:
            fallback_payload = dict(payload)
            fallback_payload["response_mode"] = "blocking"
            print("\n[RUN-BLOCKING-FALLBACK] start")
            try:
                r = client.post(run_url, json=fallback_payload, headers=headers, timeout=120.0)
                print(f"[RUN-BLOCKING-FALLBACK] status={r.status_code}")
                print(f"[RUN-BLOCKING-FALLBACK] body={r.text[:4000]}")
                if r.status_code < 400:
                    body = r.json()
                    for key in ("workflow_run_id", "run_id", "task_id"):
                        if isinstance(body.get(key), str):
                            workflow_run_id = body[key]
                    if not workflow_run_id and isinstance(body.get("data"), dict):
                        for key in ("workflow_run_id", "run_id", "task_id"):
                            if isinstance(body["data"].get(key), str):
                                workflow_run_id = body["data"][key]
            except Exception as exc:
                print(f"[RUN-BLOCKING-FALLBACK] exception={type(exc).__name__}: {exc}")

        # Step 3b: variable-name compatibility retry (legacy alias)
        if not workflow_run_id:
            retry_payload = {
                "inputs": dict(payload["inputs"]),
                "response_mode": "blocking",
                "user": user_id,
            }
            retry_payload["inputs"]["customer_phonenumber"] = retry_payload["inputs"].get("phone_number")
            print("\n[RUN-BLOCKING-RETRY-LEGACY_PHONE] start")
            print(f"[RUN-BLOCKING-RETRY-LEGACY_PHONE] payload={_pretty(retry_payload)}")
            try:
                r = client.post(run_url, json=retry_payload, headers=headers, timeout=180.0)
                print(f"[RUN-BLOCKING-RETRY-LEGACY_PHONE] status={r.status_code}")
                print(f"[RUN-BLOCKING-RETRY-LEGACY_PHONE] body={r.text[:4000]}")
                if r.status_code < 400:
                    body = r.json()
                    for key in ("workflow_run_id", "run_id", "task_id"):
                        if isinstance(body.get(key), str):
                            workflow_run_id = body[key]
                    if not workflow_run_id and isinstance(body.get("data"), dict):
                        for key in ("workflow_run_id", "run_id", "task_id"):
                            if isinstance(body["data"].get(key), str):
                                workflow_run_id = body["data"][key]
            except Exception as exc:
                print(f"[RUN-BLOCKING-RETRY-LEGACY_PHONE] exception={type(exc).__name__}: {exc}")

        if not workflow_run_id:
            print("[ERROR] no workflow_run_id acquired, unable to poll")
            return 4

        # Step 4: poll result
        poll_url = f"{base_url.rstrip('/')}/v1/workflows/run/{workflow_run_id}"
        print(f"\n[POLL] GET {poll_url}")
        deadline = time.time() + poll_timeout_sec
        while True:
            r = client.get(poll_url, headers=headers)
            print(f"[POLL] status={r.status_code}")
            body_text = r.text[:4000]
            print(f"[POLL] body={body_text}")
            if r.status_code >= 400:
                print("[ERROR] poll request failed")
                return 5
            body = r.json()
            status = ""
            if isinstance(body, dict):
                status = body.get("status", "")
                if not status and isinstance(body.get("data"), dict):
                    status = body["data"].get("status", "")
            print(f"[POLL] parsed_status={status}")
            if status in {"succeeded", "failed", "stopped"}:
                print(f"[DONE] final_status={status}")
                return 0
            if time.time() > deadline:
                print(f"[ERROR] poll timeout after {poll_timeout_sec}s")
                return 6
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(run_probe())

