"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from returnproof import __version__
from returnproof.api.claim_routes import router as claim_api_router
from returnproof.api.routes import router as api_router
from returnproof.config import Settings, get_settings
from returnproof.db import build_engine, build_session_factory
from returnproof.web.auth_routes import router as app_router
from returnproof.web.claim_routes import router as claim_router
from returnproof.web.evidence_routes import router as evidence_router
from returnproof.web.routes import router as web_router
from returnproof.web.verification_routes import router as verification_router

PACKAGE_DIR = __import__("pathlib").Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the ReturnProof ASGI application."""
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        yield
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
        openapi_tags=[
            {"name": "system", "description": "Health and readiness endpoints."},
        ],
    )
    app.state.settings = app_settings
    engine = build_engine(app_settings.database_url, echo=app_settings.debug)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
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
    app.include_router(app_router)
    app.include_router(evidence_router)
    app.include_router(verification_router)
    app.include_router(claim_router)
    app.include_router(web_router)

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
