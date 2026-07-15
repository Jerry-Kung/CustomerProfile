from __future__ import annotations

import os

import uvicorn

from src.app.asgi import app


def main() -> int:
    host = os.getenv("API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("API_PORT", "8000").strip() or "8000")
    # The application configures Uvicorn's standard loggers to flow into Loguru.
    uvicorn.run(app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

