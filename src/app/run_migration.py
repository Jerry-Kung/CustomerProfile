from __future__ import annotations

from src.config.logging_utils import get_logger
from src.config.settings import load_config
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.migration_runner import run_migration


def main() -> int:
    logger = get_logger("migration")
    config = load_config()
    paths = default_contract_paths()
    run_migration(config.mysql, paths, wait=config.migration)
    logger.info("migration_applied dir=%s", paths.migrations_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

