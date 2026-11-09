#!/usr/bin/env bash
# Use the same file for Compose interpolation and container env_file injection.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_path="$project_dir/.env"
if [[ "${1:-}" == "--env-file" ]]; then
  [[ $# -ge 2 ]] || { echo "--env-file requires a path" >&2; exit 2; }
  env_path="$2"
  shift 2
fi
[[ -f "$env_path" ]] || { echo "Environment file does not exist" >&2; exit 2; }
env_path="$(cd -- "$(dirname -- "$env_path")" && pwd)/$(basename -- "$env_path")"
export APP_ENV_FILE="$env_path"
exec docker compose --project-directory "$project_dir" --env-file "$env_path" "$@"
