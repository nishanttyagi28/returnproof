"""HTML/JSON error handling with request identifiers."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from returnproof.web.templating import render

logger = logging.getLogger("returnproof.errors")

TITLES = {
    400: "Bad request",
    401: "Sign in required",
    403: "Access denied",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Upload too large",
    422: "Check your input",
    429: "Too many attempts",
    500: "Something went wrong",
}


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False
    return "application/json" not in request.headers.get("accept", "")


def _error_page(
    request: Request,
    status_code: int,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response = render(
        request,
        "error.html",
        {
            "settings": request.app.state.settings,
            "user": None,
            "status_code": status_code,
            "title": TITLES.get(status_code, "Error"),
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        },
        status_code=status_code,
    )
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        detail = str(exc.detail) if exc.detail else TITLES.get(exc.status_code, "Error")
        if not _wants_html(request):
            return JSONResponse(
                {"detail": detail, "request_id": getattr(request.state, "request_id", None)},
                status_code=exc.status_code,
                headers=exc.headers,
            )
        message = detail
        if exc.status_code == 404:
            message = "The page you requested does not exist."
        if exc.status_code == 403:
            message = "Your role does not allow this action."
        return _error_page(request, exc.status_code, message, exc.headers)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> Response:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("unhandled_error", extra={"request_id": request_id, "path": request.url.path})
        if not _wants_html(request):
            return JSONResponse(
                {"detail": "Internal server error", "request_id": request_id},
                status_code=500,
            )
        return _error_page(
            request,
            500,
            "An unexpected error occurred. Quote the request ID when contacting support.",
        )
