from __future__ import annotations

import logging
import os
from pathlib import Path
import time

import pytest

from src.config.settings import load_config
from src.domain.task.service import TaskService
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.task_repository import MySQLTaskRepository
from src.infrastructure.dify.client import DifyClient, extract_output_string
from src.infrastructure.dify.errors import DifyError
from src.infrastructure.workflow.output_parser import WorkflowOutputParser

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_REAL_INTEGRATION") != "1",
    reason="set RUN_REAL_INTEGRATION=1 to run real MySQL/Dify integration tests",
)


def _load_env_file(env_path: Path) -> None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key and key not in os.environ:
            os.environ[key] = value


@pytest.fixture(scope="module")
def integration_context():
    env_path = Path("d:/Huawei/Projects/swt_dfms/customer_profile_backend/.env.example")
    _load_env_file(env_path)
    # Long-running workflows may take 10-15 minutes.
    os.environ["DIFY_STREAM_INIT_TIMEOUT_MS"] = "30000"
    os.environ["DIFY_POLL_INTERVAL_MS"] = "1000"
    os.environ["DIFY_POLL_TIMEOUT_MS"] = "1200000"
    config = load_config()
    logger = logging.getLogger("integration")
    logger.handlers = []
    logger.addHandler(logging.StreamHandler())
    logger.setLevel("INFO")

    db_client = MySQLClient(config.mysql)
    repo = MySQLTaskRepository(db_client)
    dify_client = DifyClient(config=config.dify)
    service = TaskService(repository=repo, logger=logger)
    return {
        "config": config,
        "repo": repo,
        "service": service,
        "dify_client": dify_client,
        "logger": logger,
    }


def _load_test_data() -> tuple[str, str, str]:
    root = Path("d:/Huawei/Projects/swt_dfms/customer_profile_backend/tests/test_data")
    phone = (root / "customer_phonenumber.txt").read_text(encoding="utf-8").strip()
    return phone, str(root / "customer_wechat_homepage.jpg"), str(root / "customer_moment_homepage.jpg")


def _clean_task(phone: str, repo: MySQLTaskRepository) -> None:
    conn = repo._db_client.connect()  # noqa: SLF001
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM customer_profile_tasks WHERE customer_phonenumber = %s", (phone,))
        conn.commit()
    finally:
        conn.close()


def _clean_prefix(phone_prefix: str, repo: MySQLTaskRepository) -> None:
    conn = repo._db_client.connect()  # noqa: SLF001
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM customer_profile_tasks
            WHERE REPLACE(customer_phonenumber, ' ', '') LIKE CONCAT(REPLACE(%s, ' ', ''), '%%')
            """,
            (phone_prefix,),
        )
        conn.commit()
    finally:
        conn.close()


def _set_task_oldest_updated_at(phone: str, repo: MySQLTaskRepository) -> None:
    conn = repo._db_client.connect()  # noqa: SLF001
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE customer_profile_tasks
            SET updated_at = '2000-01-01 00:00:00'
            WHERE customer_phonenumber = %s
            """,
            (phone,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.mysql_integration
def test_mysql_repository_basic_flow(integration_context):
    repo: MySQLTaskRepository = integration_context["repo"]
    service: TaskService = integration_context["service"]
    phone_base, _, _ = _load_test_data()
    _clean_prefix(phone_base, repo)
    phone = phone_base
    _clean_task(phone, repo)

    created = service.submit_task(phone)
    assert created.customer_phonenumber == phone
    assert created.status == "queued"
    # Shared integration DB may contain other queued rows; ensure this task is claimed first.
    _set_task_oldest_updated_at(phone, repo)

    claimed = service.claim_next_task()
    assert claimed is not None
    assert claimed.customer_phonenumber == phone
    assert claimed.status == "processing"

    time.sleep(1.2)
    requeued = service.requeue_stale_processing(timeout_seconds=0, limit=10)
    assert requeued >= 1

    fetched = service.get_task_by_phone(phone)
    assert fetched is not None
    assert fetched.status == "queued"
    assert fetched.customer_phonenumber == phone
    _clean_task(phone, repo)


@pytest.mark.integration
@pytest.mark.dify_integration
def test_dify_mysql_end_to_end_with_real_images(integration_context):
    repo: MySQLTaskRepository = integration_context["repo"]
    service: TaskService = integration_context["service"]
    dify_client: DifyClient = integration_context["dify_client"]
    logger: logging.Logger = integration_context["logger"]
    parser = WorkflowOutputParser(
        schema_path=Path("d:/Huawei/Projects/swt_dfms/customer_profile_backend/contracts/customer-profile-output.schema.json")
    )

    phone_base, wechat_img, moment_img = _load_test_data()
    assert Path(wechat_img).exists()
    assert Path(moment_img).exists()
    phone = phone_base
    _clean_task(phone, repo)
    created = service.submit_task(
        phone,
        wechat_homepage_image=wechat_img,
        wechat_moments_image=moment_img,
    )
    assert created.status == "queued"
    claimed = service.claim_next_task()
    assert claimed is not None
    assert claimed.id is not None
    assert claimed.status == "processing"
    assert claimed.wechat_homepage_image == wechat_img
    assert claimed.wechat_moments_image == moment_img

    logger.info(
        "integration_dify_start phone_suffix=%s wechat_img=%s moment_img=%s",
        phone[-4:],
        Path(wechat_img).name,
        Path(moment_img).name,
    )
    try:
        result = dify_client.run_workflow(
            customer_phonenumber=phone,
            wechat_homepage_image=claimed.wechat_homepage_image,
            wechat_moments_image=claimed.wechat_moments_image,
        )
        status = result.get("status") or (
            result.get("data", {}) if isinstance(result.get("data"), dict) else {}
        ).get("status")
        assert status in {"succeeded", "failed", "stopped"}
        if status == "succeeded":
            output_raw = extract_output_string(result)
            parsed = parser.parse(output_raw)
            repo.mark_succeeded(
                task_id=claimed.id,
                output=parsed.output,
                card_id=parsed.card_id,
                nickname=parsed.nickname,
            )
            stored = service.get_task_by_phone(phone)
            assert stored is not None
            assert stored.status == "succeeded"
            assert stored.card_id == parsed.card_id
            assert stored.nickname == parsed.nickname
        else:
            repo.mark_failed(task_id=claimed.id, error_message=f"DIFY_FINAL_STATUS: {status}")
            stored = service.get_task_by_phone(phone)
            assert stored is not None
            assert stored.status == "failed"
            assert stored.error_message is not None
            assert status in stored.error_message
        logger.info("integration_dify_done phone_suffix=%s status=%s", phone[-4:], status)
    except DifyError as exc:
        repo.mark_failed(task_id=claimed.id, error_message=f"{exc.code}: {exc}")
        stored = service.get_task_by_phone(phone)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.error_message is not None
        assert exc.code in stored.error_message
    finally:
        _clean_task(phone, repo)

