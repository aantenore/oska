"""Errors a channel reports, with the status and the wording the response carries."""

from __future__ import annotations

__all__ = ["ApiError"]


class ApiError(Exception):
    """An error with the status and the wording the response carries."""

    def __init__(self, status_code: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail
