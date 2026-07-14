from __future__ import annotations

from pathlib import Path

import pytest

from src.infrastructure.dify.client import extract_output_string
from src.infrastructure.dify.errors import DifyOutputInvalidJson
from src.infrastructure.workflow.errors import (
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "customer-profile-output.schema.json"


def test_extract_output_string_supports_top_level_output():
    body = {"output": '{"card_id":"c1","nickname":"n1"}'}
    assert extract_output_string(body) == '{"card_id":"c1","nickname":"n1"}'


def test_extract_output_string_supports_nested_output():
    body = {"data": {"outputs": {"output": '{"card_id":"c1","nickname":"n1"}'}}}
    assert extract_output_string(body) == '{"card_id":"c1","nickname":"n1"}'


def test_extract_output_string_missing_raises_dify_specific():
    """Backward compat: missing output keeps the DIFY_OUTPUT_INVALID_JSON code path live."""
    with pytest.raises(DifyOutputInvalidJson):
        extract_output_string({"data": {"something": "else"}})


def test_parse_success():
    parser = WorkflowOutputParser(schema_path=_schema_path())
    parsed = parser.parse('{"card_id":"c1","nickname":"n1"}')
    assert parsed.card_id == "c1"
    assert parsed.nickname == "n1"
    assert parsed.output["card_id"] == "c1"


def test_parse_promotes_customer_nickname_to_top_level():
    """Some workflows output nickname only under customer.nickname (rich profile JSON)."""
    parser = WorkflowOutputParser(schema_path=_schema_path())
    raw = '{"card_id":"lc_1","customer":{"nickname":"李宁/Niki"}}'
    parsed = parser.parse(raw)
    assert parsed.card_id == "lc_1"
    assert parsed.nickname == "李宁/Niki"
    assert parsed.output["nickname"] == "李宁/Niki"


def test_parse_invalid_json():
    parser = WorkflowOutputParser(schema_path=_schema_path())
    with pytest.raises(WorkflowOutputInvalidJson):
        parser.parse("{bad-json}")


def test_parse_missing_required_fields():
    parser = WorkflowOutputParser(schema_path=_schema_path())
    with pytest.raises(WorkflowOutputSchemaMismatch):
        parser.parse('{"nickname":"n1"}')
