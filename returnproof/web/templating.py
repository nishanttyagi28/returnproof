"""Jinja environment and display filters (money in paise, UTC -> IST)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.responses import Response

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
IST_OFFSET_HOURS = 5
IST_OFFSET_MINUTES = 30


def to_ist(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None) + timedelta(
        hours=IST_OFFSET_HOURS, minutes=IST_OFFSET_MINUTES
    )


def format_ist(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    converted = to_ist(value)
    if converted is None:
        return "—"
    return f"{converted.strftime(fmt)} IST"


def format_date_ist(value: datetime | None) -> str:
    return format_ist(value, "%d %b %Y")


def format_inr(paise: int | None) -> str:
    if paise is None:
        return "—"
    negative = paise < 0
    whole, fraction = divmod(abs(int(paise)), 100)
    text = f"{whole:,}"
    if fraction:
        text += f".{fraction:02d}"
    return f"{'-' if negative else ''}₹{text}"


def human_enum(value: str | None) -> str:
    if not value:
        return "—"
    return value.replace("_", " ").title()


def mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return "—"
    local, domain = value.split("@", 1)
    return f"{local[:2]}***@{domain}"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["inr"] = format_inr
templates.env.filters["ist"] = format_ist
templates.env.filters["date_ist"] = format_date_ist
templates.env.filters["humanize"] = human_enum
templates.env.filters["mask_email"] = mask_email
templates.env.globals["markup"] = Markup


def render(
    request: Any, template: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> Response:
    payload: dict[str, Any] = {"request": request}
    payload.update(context or {})
    return templates.TemplateResponse(request, template, payload, status_code=status_code)
