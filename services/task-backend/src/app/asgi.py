"""
ASGI application for production-style servers (uvicorn, gunicorn+uvicorn workers).

Usage:
  uvicorn src.app.asgi:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from src.app.bootstrap import create_application

app = create_application()
