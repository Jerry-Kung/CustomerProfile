from __future__ import annotations

from src.config.logging_utils import configure_root_logging, get_logger
from src.contracts.registry import default_contract_paths, validate_contract_baseline


def main() -> int:
    configure_root_logging("contracts")
    logger = get_logger("contracts")
    paths = default_contract_paths()
    validate_contract_baseline(paths)
    logger.info(
        "contracts_validated openapi=%s schema=%s migration=%s",
        paths.openapi_path,
        paths.output_schema_path,
        paths.migration_sql_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
