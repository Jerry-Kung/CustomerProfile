#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then
  set -- python -m customer_profile.main
fi
case "$1" in
  customer-profile-api|customer-profile-worker|customer-profile-migrate|customer-profile-check-contracts)
    export PATH="/opt/task-venv/bin:$PATH"
    export PYTHONPATH=/app/services/task-backend
    cd /app/services/task-backend
    ;;
esac
exec "$@"
