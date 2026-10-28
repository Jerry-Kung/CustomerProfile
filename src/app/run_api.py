from __future__ import annotations

import uvicorn

from src.app.asgi import app


def main() -> int:
    # The application configures Uvicorn's standard loggers to flow into Loguru.
    config = app.state.api_config
    uvicorn.run(app, host=config.host, port=config.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

