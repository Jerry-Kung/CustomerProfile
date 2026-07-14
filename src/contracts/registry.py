from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ContractPaths:
    openapi_path: Path
    output_schema_path: Path
    migration_sql_path: Path
    migrations_dir: Path


def default_contract_paths(project_root: Path | None = None) -> ContractPaths:
    root = project_root or Path(__file__).resolve().parents[2]
    migrations_dir = root / "db" / "migrations"
    return ContractPaths(
        openapi_path=root / "contracts" / "customer-profile-api.openapi.yaml",
        output_schema_path=root / "contracts" / "customer-profile-output.schema.json",
        migration_sql_path=migrations_dir / "001_create_customer_profile_tasks.sql",
        migrations_dir=migrations_dir,
    )


def load_openapi(paths: ContractPaths) -> dict[str, Any]:
    with paths.openapi_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_output_schema(paths: ContractPaths) -> dict[str, Any]:
    with paths.output_schema_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_migration_sql(paths: ContractPaths) -> str:
    return paths.migration_sql_path.read_text(encoding="utf-8")


def validate_contract_baseline(paths: ContractPaths) -> None:
    openapi = load_openapi(paths)
    if openapi.get("openapi") != "3.0.3":
        raise ValueError("openapi version mismatch, expected 3.0.3")
    if "/v1/customer-profile/tasks" not in openapi.get("paths", {}):
        raise ValueError("missing submit/list path in OpenAPI contract")

    schema = load_output_schema(paths)
    required = schema.get("required", [])
    if "card_id" not in required or "nickname" not in required:
        raise ValueError("output schema missing required fields card_id/nickname")

    migration = load_migration_sql(paths)
    if "CREATE TABLE IF NOT EXISTS customer_profile_tasks" not in migration:
        raise ValueError("migration baseline missing customer_profile_tasks table")
