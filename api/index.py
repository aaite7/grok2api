"""Vercel Serverless entry point.

Sets up the Vercel environment before importing the main application.
The Vercel Python Runtime looks for a top-level ``app`` / ``application``
variable in this file.
"""

import os
import sys
import traceback

# --- Vercel environment defaults (must run before any application imports) ---

if os.getenv("VERCEL") == "1":
    os.environ.setdefault("DATA_DIR", "/tmp/data")
    os.environ.setdefault("LOG_DIR", "/tmp/logs")
    os.environ.setdefault("LOG_FILE_ENABLED", "false")

# --- Import the FastAPI application ---

try:
    from app.main import app
except Exception:
    traceback.print_exc()
    sys.stderr.write("FATAL: failed to import app.main\n")

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "degraded", "error": "app import failed"}

    @app.get("/{path:path}")
    def catch_all(path: str):
        return JSONResponse(
            {"error": "Application startup failed. Check Vercel Function Logs."},
            status_code=500,
        )
