from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.infrastructure.workflow.errors import (
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
)


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
        try:
            parsed = json.loads(output_raw)
        except json.JSONDecodeError as exc:
            raise WorkflowOutputInvalidJson("output is not valid JSON string") from exc
        if not isinstance(parsed, dict):
            raise WorkflowOutputSchemaMismatch("output root must be object")

        parsed = normalize_profile_output_dict(parsed)

        missing = [name for name in self._required_fields if not parsed.get(name)]
        if missing:
            raise WorkflowOutputSchemaMismatch(f"output missing required fields: {', '.join(missing)}")

        card_id = parsed.get("card_id")
        nickname = parsed.get("nickname")
        if not isinstance(card_id, str) or not card_id.strip():
            raise WorkflowOutputSchemaMismatch("card_id must be non-empty string")
        if not isinstance(nickname, str) or not nickname.strip():
            raise WorkflowOutputSchemaMismatch("nickname must be non-empty string")

        return ParsedOutput(output=parsed, card_id=card_id, nickname=nickname)
