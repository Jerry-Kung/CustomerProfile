#!/usr/bin/env bash
# Default to one env file; an explicit task file keeps legacy deployments supported.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_env="${CP_ENV_FILE:-$project_dir/.env}"
task_env="${TASK_ENV_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      [[ $# -ge 2 ]] || { echo "--env-file requires a path" >&2; exit 2; }
      runtime_env="$2"; shift 2 ;;
    --task-env-file)
      [[ $# -ge 2 ]] || { echo "--task-env-file requires a path" >&2; exit 2; }
      task_env="$2"; shift 2 ;;
    *) break ;;
  esac
done
task_env="${task_env:-$runtime_env}"
[[ -f "$runtime_env" ]] || { echo "Runtime environment file does not exist: $runtime_env" >&2; exit 2; }
[[ -f "$task_env" ]] || { echo "Task environment file does not exist: $task_env" >&2; exit 2; }
export CP_ENV_FILE="$(cd -- "$(dirname -- "$runtime_env")" && pwd)/$(basename -- "$runtime_env")"
export TASK_ENV_FILE="$(cd -- "$(dirname -- "$task_env")" && pwd)/$(basename -- "$task_env")"
exec docker compose --project-directory "$project_dir" \
  --env-file "$TASK_ENV_FILE" --env-file "$CP_ENV_FILE" \
  -f "$project_dir/compose.yaml" "$@"
