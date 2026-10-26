"""统一 API、worker 与标准库日志；控制台输出由 Docker 收集。"""

from __future__ import annotations

import inspect
import logging
import sys

from loguru import logger


LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS ZZ} | {level:<8} | "
    "component={extra[component]} pid={process.id} | "
    "run_id={extra[run_id]} node_id={extra[node_id]} | "
    "{name}:{function}:{line} | {message}"
)


def _write_stderr(message: str) -> None:
    # 动态取流，避免重定向后仍向旧流（包括已关闭的测试捕获流）写入。
    sys.stderr.write(message)
    sys.stderr.flush()


class InterceptHandler(logging.Handler):
    """把 Uvicorn 等标准库日志交给 Loguru，保留调用位置及异常。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = inspect.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(level: str = "INFO", *, component: str = "api") -> None:
    """进程入口调用；重复配置替换旧处理器，不叠加输出。

    任务、节点上下文通过 ``bind`` / ``contextualize`` 设置，不写进全局配置。
    不输出异常帧的局部变量；日志级别同时约束业务和第三方日志。
    """
    normalized_level = level.upper()
    logger.level(normalized_level)  # 错误配置在启动时直接报出，避免静默失效。
    logger.configure(
        handlers=[{
            "sink": _write_stderr,
            "level": normalized_level,
            "format": LOG_FORMAT,
            "colorize": False,
            "backtrace": False,
            "diagnose": False,
        }],
        extra={"component": component, "run_id": "-", "node_id": "-"},
    )
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.NOTSET, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers.clear()
        # 请求尝试由留痕层记录；避免 HTTP 客户端 INFO/DEBUG 额外输出客户 URL。
        standard_logger.setLevel(logging.WARNING if name in {"httpx", "httpcore"} else logging.NOTSET)
        standard_logger.propagate = True
