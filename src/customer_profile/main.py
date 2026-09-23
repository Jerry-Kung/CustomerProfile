"""uvicorn 入口。

    python -m customer_profile.main

或

    uvicorn customer_profile.main:app --host 0.0.0.0 --port 8000
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
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
