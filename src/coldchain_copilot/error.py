"""Errors that tools return to the LLM.

A failed tool call returns ``{"error": {"code": ..., "message": ...}}`` rather
than raising into the agent loop. The message says what went wrong and how to
fix the call, so the model can retry on its own. Codes follow
design/interfaces.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable error codes the LLM can branch on."""

    SCOPE_REQUIRED = "SCOPE_REQUIRED"
    """A sensor tool was called without a fleet, reefer, or shipment."""
    WINDOW_TOO_LARGE = "WINDOW_TOO_LARGE"
    """The requested window exceeds ``MAX_WINDOW_HOURS``."""
    NOT_FOUND = "NOT_FOUND"
    """An unknown shipment, reefer, or clause id."""
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    """The search index is not ready, or no chunks exist for the contract version."""
    QUERY_REJECTED = "QUERY_REJECTED"
    """``run_aggregation`` used a forbidden stage or collection."""
    QUERY_FAILED = "QUERY_FAILED"
    """The database returned an error; its message is included."""


class ToolError(Exception):
    """A tool failure the LLM should see and act on.

    Attributes:
        code: Machine-readable error code.
        message: What went wrong and how to fix the call.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        """Returns the error in the shape tools send back to the LLM."""
        return {"error": {"code": str(self.code), "message": self.message}}