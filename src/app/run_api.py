from __future__ import annotations

import os

import uvicorn

from src.app.asgi import app


def main() -> int:
    host = os.getenv("API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("API_PORT", "8000").strip() or "8000")
    uvicorn.run(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

