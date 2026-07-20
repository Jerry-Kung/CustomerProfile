from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.infrastructure.workflow.errors import (
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
)
from src.config.observability import elapsed_ms


_log = logging.getLogger(__name__)


def normalize_profile_output_dict(data: dict[str, Any]) -> dict[str, Any]:
    """
    Align workflow JSON with contract-required top-level fields.

    Some workflows put `nickname` under `customer.nickname` while the API contract
    and DB extraction expect top-level `nickname` (and `card_id`).
    """
    out = dict(data)
    nick = out.get("nickname")
    if not (isinstance(nick, str) and nick.strip()):
        customer = out.get("customer")
        if isinstance(customer, dict):
            nested = customer.get("nickname")
            if isinstance(nested, str) and nested.strip():
                out["nickname"] = nested.strip()
    return out


@dataclass
class ParsedOutput:
    output: dict[str, Any]
    card_id: str | None
    nickname: str | None


class WorkflowOutputParser:
    def __init__(self, schema_path: Path):
        self._schema_path = schema_path

    def parse(self, output_raw: str) -> ParsedOutput:
        start = time.perf_counter()
        try:
            parsed = json.loads(output_raw)
        except json.JSONDecodeError as exc:
            _log.warning(
                "workflow_output_invalid_json output_size_bytes=%s json_error_position=%s elapsed_ms=%s",
                len(output_raw.encode("utf-8")),
                exc.pos,
                elapsed_ms(start),
            )
            raise WorkflowOutputInvalidJson("output is not valid JSON string") from exc
        if not isinstance(parsed, dict):
            _log.warning(
                "workflow_output_schema_mismatch reason=root_not_object root_type=%s elapsed_ms=%s",
                type(parsed).__name__,
                elapsed_ms(start),
            )
            raise WorkflowOutputSchemaMismatch("output root must be object")

        parsed = normalize_profile_output_dict(parsed)

        card_id = _optional_non_empty_string(parsed.get("card_id"))
        nickname = _optional_non_empty_string(parsed.get("nickname"))

        _log.info(
            "workflow_output_validated output_keys=%s output_size_bytes=%s elapsed_ms=%s",
            sorted(parsed.keys()),
            len(output_raw.encode("utf-8")),
            elapsed_ms(start),
        )
        return ParsedOutput(output=parsed, card_id=card_id, nickname=nickname)


def _optional_non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value.strip() else None
