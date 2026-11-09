#!/usr/bin/env bash
# Run against a live API (default http://127.0.0.1:8000). Requires curl and jq (optional for pretty print).
set -euo pipefail

BASE="${API_E2E_BASE_URL:-http://127.0.0.1:8000}"
BASE="${BASE%/}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/tests/test_data"
PHONE_RAW="$(tr -d '\r' < "$DATA/customer_phonenumber.txt" | xargs)"
# API allows only digits and +; strip spaces
PHONE="${PHONE_RAW// /}"

WECHAT="$DATA/customer_wechat_homepage.jpg"
MOMENT="$DATA/customer_moment_homepage.jpg"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi

echo "== GET $BASE/health"
curl -sS -f "$BASE/health" | { command -v jq >/dev/null 2>&1 && jq . || cat -; }
echo

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

python3 -c "
import json
from pathlib import Path
p = Path(r'''$TMP''')
payload = {
  'customer_phonenumber': '''$PHONE''',
  'wechat_homepage_image': Path(r'''$WECHAT''').resolve().as_posix(),
  'wechat_moments_image': Path(r'''$MOMENT''').resolve().as_posix(),
}
p.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
"

echo "== POST $BASE/v1/customer-profile/tasks"
curl -sS -f -X POST "$BASE/v1/customer-profile/tasks" \
  -H "Content-Type: application/json" \
  -d @"$TMP" | { command -v jq >/dev/null 2>&1 && jq . || cat -; }
echo

PREFIX="${PHONE:0:3}"
echo "== GET $BASE/v1/customer-profile/tasks?phone_prefix=$PREFIX&page=1&page_size=5"
curl -sS -f "$BASE/v1/customer-profile/tasks?phone_prefix=${PREFIX}&page=1&page_size=5" | { command -v jq >/dev/null 2>&1 && jq . || cat -; }
echo

ENC_PHONE=$(python3 -c "import urllib.parse; print(urllib.parse.quote('''$PHONE''', safe=''))")
echo "== GET $BASE/v1/customer-profile/tasks/${ENC_PHONE}/result"
curl -sS "$BASE/v1/customer-profile/tasks/${ENC_PHONE}/result" -w "\nHTTP_CODE:%{http_code}\n" | head -n 50

echo "Done."
