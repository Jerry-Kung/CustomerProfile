from __future__ import annotations

from pathlib import Path

from src.contracts.registry import default_contract_paths, validate_contract_baseline

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_default_contract_paths_exist():
    paths = default_contract_paths(_PROJECT_ROOT)
    assert paths.openapi_path.exists()
    assert paths.output_schema_path.exists()
    assert paths.migration_sql_path.exists()


def test_validate_contract_baseline_success():
    paths = default_contract_paths(_PROJECT_ROOT)
    validate_contract_baseline(paths)
