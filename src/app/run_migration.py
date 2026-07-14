from __future__ import annotations

from src.config.logging_utils import configure_root_logging, get_logger, log_runtime_identity
from src.config.settings import load_config
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.migration_runner import run_migration


def main() -> int:
    configure_root_logging()
    logger = get_logger("migration")
    config = load_config()
    log_runtime_identity(logger, component="migration", app_env=config.app_env)
    paths = default_contract_paths()
    run_migration(config.mysql, paths, wait=config.migration)
    logger.info("migration_applied dir=%s", paths.migrations_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

