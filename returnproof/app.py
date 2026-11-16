"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from returnproof import __version__
from returnproof.api.claim_routes import router as claim_api_router
from returnproof.api.export_routes import router as export_api_router
from returnproof.api.job_routes import router as job_api_router
from returnproof.api.recovery_routes import router as recovery_api_router
from returnproof.api.routes import router as api_router
from returnproof.config import Settings, get_settings
from returnproof.db import build_engine, build_session_factory
from returnproof.errors import register_error_handlers
from returnproof.jobs import InProcessJobRunner
from returnproof.logging_config import configure_logging
from returnproof.ratelimit import SlidingWindowRateLimiter
from returnproof.web.auth_routes import router as app_router
from returnproof.web.claim_routes import router as claim_router
from returnproof.web.evidence_routes import router as evidence_router
from returnproof.web.ops_routes import router as ops_router
from returnproof.web.routes import router as web_router
from returnproof.web.settlement_routes import router as settlement_router
from returnproof.web.verification_routes import router as verification_router

PACKAGE_DIR = Path(__file__).resolve().parent

OPENAPI_TAGS = [
    {"name": "system", "description": "Health and readiness endpoints."},
    {"name": "orders", "description": "Organisation-scoped orders and order items."},
    {"name": "returns", "description": "Return requests and verification results."},
    {"name": "evidence", "description": "Evidence uploads, hash chains and tamper checks."},
    {"name": "verification", "description": "Product identity verification and CSV import."},
    {"name": "claims", "description": "Claim packs, templates and lifecycle transitions."},
    {"name": "recovery", "description": "Settlement imports, leakage detection and recovery ledger."},
    {"name": "exports", "description": "Formula-safe CSV exports."},
    {"name": "jobs", "description": "In-process background jobs for imports and packs."},
    {"name": "api-keys", "description": "Organisation API key management."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the ReturnProof ASGI application."""
    app_settings = settings or get_settings()
    configure_logging("DEBUG" if app_settings.debug else "INFO")
    access_logger = logging.getLogger("returnproof.access")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        yield
        app.state.jobs.shutdown()
        app.state.engine.dispose()

    app = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        description=(
            "Evidence-backed returns, product-identity verification and "
            "financial-recovery operations for Indian e-commerce sellers."
        ),
        debug=app_settings.debug,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.settings = app_settings
    engine = build_engine(app_settings.database_url, echo=app_settings.debug)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.jobs = InProcessJobRunner()
    app.state.login_limiter = SlidingWindowRateLimiter(
        limit=app_settings.rate_limit_auth_per_minute,
        window_seconds=60,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: object) -> object:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)  # type: ignore[operator]
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        access_logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.secret_key,
        session_cookie=app_settings.session_cookie,
        max_age=app_settings.session_max_age_seconds,
        same_site="lax",
        https_only=app_settings.is_production,
    )
    app.include_router(api_router)
    app.include_router(claim_api_router)
    app.include_router(recovery_api_router)
    app.include_router(export_api_router)
    app.include_router(job_api_router)
    app.include_router(app_router)
    app.include_router(evidence_router)
    app.include_router(verification_router)
    app.include_router(claim_router)
    app.include_router(settlement_router)
    app.include_router(ops_router)
    app.include_router(web_router)
    register_error_handlers(app)

    @app.get("/healthz", tags=["system"], summary="Liveness probe")
    def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "app": app_settings.app_name,
                "version": __version__,
                "environment": app_settings.environment,
            }
        )

    return app


app = create_app()
