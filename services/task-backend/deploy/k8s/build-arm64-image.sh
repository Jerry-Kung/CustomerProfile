#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-registry.dfmc.com.cn/mhero-ai/customer-profile-backend:arm64-latest}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
MODE="${MODE:-push}"

case "${MODE}" in
  push)
    OUTPUT_FLAG="--push"
    ;;
  load)
    OUTPUT_FLAG="--load"
    ;;
  *)
    echo "MODE must be push or load" >&2
    exit 2
    ;;
esac

docker buildx build \
  --platform linux/arm64 \
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}" \
  -t "${IMAGE}" \
  ${OUTPUT_FLAG} \
  .
