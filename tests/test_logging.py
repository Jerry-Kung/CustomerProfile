"""日志契约：级别、异常原文、并发上下文与 Uvicorn 转发。

日志初始化会修改全局状态，因此每个用例在独立子进程中运行。
子进程工作目录是临时目录，不读取开发者的 .env。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _run_logging_script(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "logging_probe.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("level", ["INFO", "WARNING"])
def test_level_filters_loguru_and_stdlib_and_preserves_chinese(tmp_path, level):
    result = _run_logging_script(
        tmp_path,
        f"""
        import logging
        from loguru import logger
        from customer_profile.logging_config import configure_logging

        configure_logging({level!r}, component="worker")
        logger.debug("loguru-debug")
        logger.info("loguru-info 中文画像")
        logger.warning("loguru-warning")
        logging.getLogger("external.client").debug("stdlib-debug")
        logging.getLogger("external.client").info("stdlib-info 中文证据")
        logging.getLogger("external.client").warning("stdlib-warning")
        """,
    )
    assert result.stdout == ""
    assert "loguru-debug" not in result.stderr
    assert "stdlib-debug" not in result.stderr
    assert result.stderr.count("loguru-warning") == 1
    assert result.stderr.count("stdlib-warning") == 1
    if level == "INFO":
        assert result.stderr.count("loguru-info 中文画像") == 1
        assert result.stderr.count("stdlib-info 中文证据") == 1
    else:
        assert "loguru-info" not in result.stderr
        assert "stdlib-info" not in result.stderr
    assert "component=worker" in result.stderr
    assert "run_id=-" in result.stderr
    assert "node_id=-" in result.stderr
    assert "\ufffd" not in result.stderr


def test_repeated_initialization_does_not_duplicate_uvicorn_logs(tmp_path):
    result = _run_logging_script(
        tmp_path,
        """
        import logging
        import sys
        from customer_profile.logging_config import configure_logging

        names = ("uvicorn", "uvicorn.error", "uvicorn.access")
        for name in names:
            old_logger = logging.getLogger(name)
            old_logger.addHandler(logging.StreamHandler(sys.stdout))
            old_logger.propagate = False
        configure_logging("INFO", component="api")
        configure_logging("INFO", component="api")
        for name in names:
            logging.getLogger(name).warning("one-event-%s", name)
        """,
    )
    assert result.stdout == ""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert sum(line.endswith(f"one-event-{name}") for line in result.stderr.splitlines()) == 1
    assert "component=api" in result.stderr


@pytest.mark.parametrize("source", ["loguru", "stdlib"])
def test_exception_preserves_traceback_without_local_values(tmp_path, source):
    result = _run_logging_script(
        tmp_path,
        f"""
        import logging
        from loguru import logger
        from customer_profile.logging_config import configure_logging

        configure_logging("INFO", component="worker")

        def fail_with_private_local():
            sensitive_test_value = {{"credential": "private-value-must-stay-hidden"}}
            return sensitive_test_value["missing-key"]

        try:
            fail_with_private_local()
        except KeyError as exc:
            if {source!r} == "loguru":
                logger.opt(exception=exc).error("异常原文：任务执行失败")
            else:
                logging.getLogger("external.client").exception("异常原文：任务执行失败")
        """,
    )
    assert "异常原文：任务执行失败" in result.stderr
    assert "Traceback (most recent call last):" in result.stderr
    assert "fail_with_private_local" in result.stderr
    assert "KeyError: 'missing-key'" in result.stderr
    assert "private-value-must-stay-hidden" not in result.stderr
    assert "ERROR" in result.stderr


def test_concurrent_contexts_do_not_mix_and_defaults_return_after_exit(tmp_path):
    result = _run_logging_script(
        tmp_path,
        """
        import asyncio
        import logging
        from loguru import logger
        from customer_profile.logging_config import configure_logging

        configure_logging("INFO", component="worker")

        async def report(run_id, node_id):
            with logger.contextualize(run_id=run_id, node_id=node_id):
                await asyncio.sleep(0)
                logger.info("loguru-context-{}", run_id)
                await asyncio.sleep(0)
                logging.getLogger("external.client").info("stdlib-context-%s", run_id)

        async def main():
            await asyncio.gather(report("run-A", "node-A"), report("run-B", "node-B"))
            logger.info("default-loguru-context")
            logging.getLogger("external.client").info("default-stdlib-context")

        asyncio.run(main())
        """,
    )
    lines = result.stderr.splitlines()
    for suffix in ("A", "B"):
        for source in ("loguru", "stdlib"):
            matched = [line for line in lines if line.endswith(f"{source}-context-run-{suffix}")]
            assert len(matched) == 1
            assert f"run_id=run-{suffix}" in matched[0]
            assert f"node_id=node-{suffix}" in matched[0]
            other_suffix = "B" if suffix == "A" else "A"
            assert f"run_id=run-{other_suffix}" not in matched[0]
    for source in ("loguru", "stdlib"):
        matched = [line for line in lines if line.endswith(f"default-{source}-context")]
        assert len(matched) == 1
        assert "run_id=-" in matched[0]
        assert "node_id=-" in matched[0]


def test_stdlib_logs_point_to_original_caller(tmp_path):
    result = _run_logging_script(
        tmp_path,
        """
        import logging
        from customer_profile.logging_config import configure_logging

        configure_logging("INFO", component="api")

        def original_log_caller():
            logging.getLogger("external.client").warning("caller-attribution-event")

        original_log_caller()
        """,
    )
    call_line = next(
        line_number
        for line_number, line in enumerate(
            (tmp_path / "logging_probe.py").read_text(encoding="utf-8").splitlines(), start=1
        )
        if 'logging.getLogger("external.client").warning' in line
    )
    assert f"__main__:original_log_caller:{call_line}" in result.stderr
    assert result.stderr.count("caller-attribution-event") == 1


def test_sink_uses_current_stderr_after_initialization(tmp_path):
    result = _run_logging_script(
        tmp_path,
        """
        import io
        import logging
        import sys
        from loguru import logger
        from customer_profile.logging_config import configure_logging

        configure_logging("INFO", component="api")
        redirected = io.StringIO()
        sys.stderr = redirected
        logger.info("dynamic-loguru-stderr")
        logging.getLogger("external.client").info("dynamic-stdlib-stderr")
        print(redirected.getvalue(), end="")
        """,
    )
    assert result.stderr == ""
    assert result.stdout.count("dynamic-loguru-stderr") == 1
    assert result.stdout.count("dynamic-stdlib-stderr") == 1
