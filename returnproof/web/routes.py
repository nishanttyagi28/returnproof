"""Public landing page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from returnproof.web.templating import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> Response:
    return render(
        request,
        "index.html",
        {
            "settings": request.app.state.settings,
            "version": __import__("returnproof").__version__,
            "user": None,
        },
    )
