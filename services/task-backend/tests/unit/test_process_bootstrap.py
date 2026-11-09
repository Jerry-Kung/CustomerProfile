import logging

from src.app import bootstrap as application
from src.config import settings


def test_api_builds_without_workflow_clients_or_upload_directory(tmp_path, monkeypatch):
    for key, value in {"MYSQL_HOST": "invalid.example", "MYSQL_USER": "test", "MYSQL_PASSWORD": "test-only",
                       "MYSQL_DATABASE": "test", "TASK_UPLOAD_DIR": str(tmp_path / "unused-uploads")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: None)
    monkeypatch.setattr(application, "configure_root_logging", lambda component: None)
    monkeypatch.setattr(application, "log_config_summary", lambda *args: None)
    monkeypatch.setattr(application, "log_runtime_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(application, "_warn_if_task_type_column_missing", lambda *args: None)
    def forbidden(*args):
        raise AssertionError("API must not initialize an execution adapter")
    monkeypatch.setattr(application, "_build_workflow_client", forbidden)
    app = application.create_application()
    assert app.state.task_service is not None
    assert not (tmp_path / "unused-uploads").exists()
    assert not hasattr(app.state, "upload_store")


def test_migration_loads_dotenv_before_logging_and_ignores_missing_workflows(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    from src.app import run_migration
    env = tmp_path / "migration.env"
    env.write_text("MYSQL_HOST=invalid.example\nMYSQL_USER=test\nMYSQL_PASSWORD=test-only\nMYSQL_DATABASE=test\n"
                   "LOG_DIR=" + str(tmp_path / "migration-logs") + "\nWORKFLOW_PLATFORM=unavailable\nWORKER_CONCURRENCY=invalid\n")
    monkeypatch.setenv("LOG_DIR", "guard-for-fixture-cleanup")
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.setattr(settings, "_load_dotenv_from_project_root", lambda: load_dotenv(env, override=False))
    observed = []
    monkeypatch.setattr(run_migration, "configure_root_logging", lambda component: observed.append(settings.env_value("LOG_DIR")))
    monkeypatch.setattr(run_migration, "get_logger", lambda name: logging.getLogger(name))
    monkeypatch.setattr(run_migration, "log_runtime_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_migration, "run_migration", lambda *args, **kwargs: observed.append("migrated"))
    # Ensure fixture environment changes are reverted by pytest, including dotenv's additions.
    for name in ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "WORKFLOW_PLATFORM", "WORKER_CONCURRENCY"):
        monkeypatch.setenv(name, {"MYSQL_HOST":"invalid.example", "MYSQL_USER":"test", "MYSQL_PASSWORD":"test-only",
                                 "MYSQL_DATABASE":"test", "WORKFLOW_PLATFORM":"unavailable", "WORKER_CONCURRENCY":"invalid"}[name])
    assert run_migration.main() == 0
    assert observed == [str(tmp_path / "migration-logs"), "migrated"]
