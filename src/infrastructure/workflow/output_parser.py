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
    card_id: str
    nickname: str


class WorkflowOutputParser:
    def __init__(self, schema_path: Path):
        self._schema_path = schema_path
        self._required_fields = self._load_required_fields(schema_path)

    @staticmethod
    def _load_required_fields(schema_path: Path) -> set[str]:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = schema.get("required", [])
        return set(required if isinstance(required, list) else [])

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

        missing = [name for name in self._required_fields if not parsed.get(name)]
        if missing:
            _log.warning(
                "workflow_output_schema_mismatch reason=missing_required_fields missing=%s output_keys=%s elapsed_ms=%s",
                sorted(missing),
                sorted(parsed.keys()),
                elapsed_ms(start),
            )
            raise WorkflowOutputSchemaMismatch(f"output missing required fields: {', '.join(missing)}")

        card_id = parsed.get("card_id")
        nickname = parsed.get("nickname")
        if not isinstance(card_id, str) or not card_id.strip():
            _log.warning(
                "workflow_output_schema_mismatch reason=invalid_card_id value_type=%s elapsed_ms=%s",
                type(card_id).__name__,
                elapsed_ms(start),
            )
            raise WorkflowOutputSchemaMismatch("card_id must be non-empty string")
        if not isinstance(nickname, str) or not nickname.strip():
            _log.warning(
                "workflow_output_schema_mismatch reason=invalid_nickname value_type=%s elapsed_ms=%s",
                type(nickname).__name__,
                elapsed_ms(start),
            )
            raise WorkflowOutputSchemaMismatch("nickname must be non-empty string")

        _log.info(
            "workflow_output_validated output_keys=%s output_size_bytes=%s elapsed_ms=%s",
            sorted(parsed.keys()),
            len(output_raw.encode("utf-8")),
            elapsed_ms(start),
        )
        return ParsedOutput(output=parsed, card_id=card_id, nickname=nickname)
