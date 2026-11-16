"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from returnproof import __version__
from returnproof.config import Settings, get_settings
from returnproof.web.routes import router as web_router

PACKAGE_DIR = __import__("pathlib").Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the ReturnProof ASGI application."""
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        yield

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
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
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
