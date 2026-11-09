"""Submit one task through the backend API without editing this script."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Customer phone number")
    parser.add_argument("--batch-id", required=True, help="Business batch identifier")
    parser.add_argument("--base-url", default="http://127.0.0.1:18008", help="Backend API base URL")
    parser.add_argument("--work-env", choices=("test", "production"), default="test")
    parser.add_argument("--task-type", choices=("customer_profile", "invitation_strategy"), default="customer_profile")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.phone.strip() or not args.batch_id.strip():
        parser.error("--phone and --batch-id must not be blank")
    parts = urllib.parse.urlsplit(args.base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        parser.error("--base-url must be an HTTP(S) base URL without a query or fragment")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    form = {
        "customer_phonenumber": args.phone,
        "batch_id": args.batch_id,
        "work_env": args.work_env,
        "task_type": args.task_type,
    }
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/v1/customer-profile/tasks",
        data=urllib.parse.urlencode(form).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            status = response.status
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"network error: {exc}")
        return 1
    print(f"status: {status}")
    try:
        print(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
