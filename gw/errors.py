"""Error types shared by every surface."""

from __future__ import annotations

from typing import Any


class GwError(Exception):
    """Base error; `status` maps to the HTTP problem+json status."""

    status = 400
    title = "Bad request"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def problem(self) -> dict[str, Any]:
        return {"type": "about:blank", "title": self.title, "status": self.status,
                "detail": self.detail, **self.extra}


class Forbidden(GwError):
    """A hard limit was hit."""

    status = 403
    title = "Forbidden"

    def __init__(self, detail: str, limit: str) -> None:
        super().__init__(detail, limit=limit)
        self.limit = limit


class NotAuthorised(GwError):
    status = 403
    title = "Not authorised"


class NotFound(GwError):
    status = 404
    title = "Not found"


class Conflict(GwError):
    """Invalid state transition; carries the allowed transitions."""

    status = 409
    title = "Conflict"

    def __init__(self, detail: str, allowed: list[str] | None = None) -> None:
        super().__init__(detail, allowed=allowed or [])


class Unprocessable(GwError):
    """Schema failure; carries a JSON pointer to the failing value."""

    status = 422
    title = "Unprocessable entity"

    def __init__(self, detail: str, pointer: str = "", warnings: list[str] | None = None) -> None:
        super().__init__(detail, pointer=pointer, warnings=warnings or [])
        self.pointer = pointer
