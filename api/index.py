"""Vercel Serverless entry point — minimal startup, no background tasks."""

import asyncio
import os
import sys
import traceback

# --- Ensure project root is on PYTHONPATH so 'app' package is importable ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# --- Vercel environment defaults (must run before any application imports) ---

if os.getenv("VERCEL") == "1":
    os.environ.setdefault("DATA_DIR", "/tmp/data")
    os.environ.setdefault("LOG_DIR", "/tmp/logs")
    os.environ.setdefault("LOG_FILE_ENABLED", "false")

# --- Minimal FastAPI app for Vercel ---

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import app.platform.logging.logger as logmod

logmod.setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    file_logging=os.getenv("LOG_FILE_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "on"},
)
logger = logmod.logger


@asynccontextmanager
async def vercel_lifespan(app: FastAPI):
    from app.platform.config.snapshot import config
    from app.platform.storage import reconcile_local_media_cache_async

    logger.info("vercel cold start: python={}", sys.version.split()[0])

    try:
        await config.load()
        logger.info("config loaded: account_storage={}", os.getenv("ACCOUNT_STORAGE", "local"))
    except Exception as exc:
        logger.error("config load failed: {}", exc)

    try:
        from app.dataplane.account import get_account_directory
        from app.control.account.backends.factory import (
            create_repository,
            describe_repository_target,
        )
        from app.control.account.runtime import reconcile_refresh_runtime

        storage_backend, storage_target = describe_repository_target()
        logger.info("account storage: backend={} target={}", storage_backend, storage_target)

        repo = create_repository()
        await asyncio.wait_for(repo.initialize(), timeout=15)
        app.state.repository = repo

        await config.load()
        await reconcile_local_media_cache_async()

        directory = await get_account_directory(repo)
        app.state.directory = directory

        logger.info("account directory ready: size={}", directory.size)

        reconcile_refresh_runtime(False)
    except Exception as exc:
        logger.error("account init failed: {}", exc)
        traceback.print_exc()

    try:
        from app.control.proxy import get_proxy_directory
        proxy_dir = await get_proxy_directory()
        logger.info("proxy directory loaded: mode={}", proxy_dir.egress_mode)
    except Exception as exc:
        logger.error("proxy init failed: {}", exc)

    logger.info("vercel startup completed")
    yield

    try:
        repo = getattr(app.state, "repository", None)
        if repo is not None:
            await repo.close()
    except Exception:
        pass
    logger.info("vercel shutdown completed")


def create_vercel_app() -> FastAPI:
    from app.platform.meta import get_project_version
    from app.platform.errors import AppError
    from fastapi.exceptions import RequestValidationError
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(
        title="Grok2API",
        version=get_project_version(),
        description="OpenAI-compatible API gateway for Grok",
        lifespan=vercel_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = tuple(first.get("loc") or ())
        param_parts = [
            str(p) for p in loc
            if str(p) not in {"body", "query", "path", "header", "cookie"}
        ]
        param = ".".join(param_parts)
        message = first.get("msg") or "Request validation failed"
        payload = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_value",
            }
        }
        if param:
            payload["error"]["param"] = param
        return JSONResponse(payload, status_code=400)

    @app.exception_handler(Exception)
    async def _generic_error_handler(request: Request, exc: Exception):
        logger.error("unhandled exception: {}", exc)
        return JSONResponse(
            {"error": {"message": "Internal server error", "type": "server_error"}},
            status_code=500,
        )

    from app.products.web import router as web_router
    from app.products.openai.router import router as openai_router
    from app.products.anthropic.router import router as anthropic_router

    app.include_router(web_router)
    app.include_router(openai_router)
    app.include_router(anthropic_router)

    _statics_dir = Path(__file__).resolve().parents[1] / "app" / "statics"
    if _statics_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_statics_dir)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        from fastapi.responses import FileResponse
        ico = _statics_dir / "favicon.ico"
        if ico.exists():
            return FileResponse(str(ico))
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    return app


app = create_vercel_app()
