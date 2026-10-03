"""uvicorn 入口。

    python -m customer_profile.main

或

    uvicorn customer_profile.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from .api import create_app

app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "customer_profile.main:app",
        host="0.0.0.0",
        port=8000,
        log_config=None,  # create_app 已统一配置 Loguru，避免 Uvicorn 覆盖或重复输出。
    )


if __name__ == "__main__":
    main()
