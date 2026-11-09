#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_python="${RUNTIME_PYTHON:-$project_dir/.venv/bin/python}"
task_python="${TASK_PYTHON:-$project_dir/services/task-backend/.venv/bin/python}"
export PYTHONDONTWRITEBYTECODE=1
cd "$project_dir"
"$runtime_python" -B -m pytest tests -o addopts= -q "$@"
cd "$project_dir/services/task-backend"
"$task_python" -B -m pytest tests/unit -o addopts= -q "$@"
"$task_python" -B -m src.contracts.validate_contracts
RUN_NATIVE_INVITATION_CONTRACT=1 \
  "$runtime_python" -B -m pytest tests/integration/test_native_invitation_contract.py -o addopts= -q "$@"
