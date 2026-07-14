"""Submit a single customer-profile task to the local API for testing.

Assumes the API is reachable on the same host (defaults to http://127.0.0.1:8000).
Fill in CUSTOMER_PHONENUMBER and BATCH_ID below, then run:

    python scripts/submit_task_curl.py

The endpoint accepts multipart/form-data OR application/x-www-form-urlencoded
(FastAPI `Form(...)` parameters). This script uses x-www-form-urlencoded
because there are no image files to attach.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


API_BASE_URL = "http://127.0.0.1:8000"

CUSTOMER_PHONENUMBER = ""
BATCH_ID = ""

WORK_ENV = "production"
TASK_TYPE = "customer_profile"


def main() -> int:
    if not CUSTOMER_PHONENUMBER:
        print("ERROR: CUSTOMER_PHONENUMBER is empty. Edit the script and set it.")
        return 2
    if not BATCH_ID:
        print("ERROR: BATCH_ID is empty. Edit the script and set it.")
        return 2

    url = f"{API_BASE_URL.rstrip('/')}/v1/customer-profile/tasks"
    form = {
        "customer_phonenumber": CUSTOMER_PHONENUMBER,
        "work_env": WORK_ENV,
        "batch_id": BATCH_ID,
        "task_type": TASK_TYPE,
    }
    body = urllib.parse.urlencode(form).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    print(f"POST {url}")
    print(f"form: {form}")
    print()

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.status
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        payload = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"network error: {exc}")
        return 1

    print(f"status: {status_code}")
    try:
        print("response:")
        print(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(f"response (raw): {payload}")
    return 0 if 200 <= status_code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
