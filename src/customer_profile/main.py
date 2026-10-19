"""uvicorn 入口。

    python -m customer_profile.main

或

    uvicorn customer_profile.main:app --host 0.0.0.0 --port 18173

监听地址与端口取自配置（``API_HOST`` / ``API_PORT``，缺省 ``0.0.0.0:18173``）。
**不要在这里写死端口**：容器部署时宿主机映射与健康检查都跟着同一个值走，
两处各写一份会让「部署机上端口被占用」只能靠改代码解决。
"""

from __future__ import annotations

from .api import create_app
from .settings import get_settings

app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "customer_profile.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # 日志已由 create_app 统一交给 Loguru。
    )


if __name__ == "__main__":
    main()
