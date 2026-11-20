"""Scope and time-window resolution shared by every tool.

Every sensor tool answers two questions before it touches the readings:
which readings (scope) and over what period (window). This module answers
both the same way for every tool, following design/interfaces.md
"Common Rules":

- Scope is required. ``fleet_id``, ``reefer_id``, and ``shipment_id`` map to
  the reading's meta fields; a call with none of them is rejected, never
  widened to the whole fleet.
- A fleet or reefer scope takes ``hours`` (default 24, ending now) or an
  explicit ``start`` / ``end``, at most ``MAX_WINDOW_HOURS`` long.
- A shipment scope ignores those and uses the shipment period, so cumulative
  measures cover the whole shipment.

Typical usage inside a tool:

    from coldchain_copilot.tools import scope

    meta, start, end = scope.resolve(fleet_id="FLEET-01", hours=12)
    events = detect_temperature_excursion(meta, start, end)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from coldchain_copilot import db
from coldchain_copilot.config import get_settings
from coldchain_copilot.errors import ErrorCode, ToolError

Doc = dict[str, Any]

DEFAULT_HOURS = 24
"""Window length when a fleet or reefer call gives neither hours nor times."""


def meta_filter(
    fleet_id: str | None = None,
    reefer_id: str | None = None,
    shipment_id: str | None = None,
) -> Doc:
    """Builds the readings filter for the given scope.

    Each id the caller gives becomes one condition on the reading's meta.
    Several ids narrow the scope together (for example a fleet and a reefer
    in it), because the conditions are combined with AND.

    Args:
        fleet_id: Fleet to include, matched against ``meta.fleetId``.
        reefer_id: Reefer to include, matched against ``meta.reeferId``.
        shipment_id: Shipment to include, matched against ``meta.shipmentId``.

    Returns:
        A filter such as ``{"meta.fleetId": "FLEET-01"}``, usable directly as
        the ``scope`` argument of every detector.

    Raises:
        ToolError: ``SCOPE_REQUIRED`` when no id is given.
    """
    conditions = {
        "meta.fleetId": fleet_id,
        "meta.reeferId": reefer_id,
        "meta.shipmentId": shipment_id,
    }
    scope = {field: value for field, value in conditions.items() if value}
    if not scope:
        raise ToolError(ErrorCode.SCOPE_REQUIRED, "Provide fleet_id, reefer_id, or shipment_id.")
    return scope


def parse_time(value: str) -> datetime:
    """Parses an ISO 8601 time from a tool call into an aware UTC datetime.

    Any offset is accepted (``Z``, ``+09:00``, ...). A time without an offset
    is read as UTC, the same zone the database stores. The tool schemas ask
    the LLM for an offset, so this is a fallback, not the expected input.

    Args:
        value: The time string, for example ``"2026-10-15T09:12:00+09:00"``.

    Returns:
        The same instant as a timezone-aware datetime in UTC.

    Raises:
        ValueError: If the string is not ISO 8601.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def window(
    hours: float | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    max_hours: float,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolves the window of a fleet- or reefer-scoped call.

    Explicit times win over ``hours``. When only one of ``start`` and
    ``end`` is given, the other is filled in: a missing end is now, a missing
    start is ``DEFAULT_HOURS`` before the end. With no times at all, the
    window is the last ``hours`` (default 24) ending now.

    Args:
        hours: Length of a window that ends now.
        start: Window start, ISO 8601.
        end: Window end, ISO 8601.
        max_hours: Longest allowed window, normally ``MAX_WINDOW_HOURS``.
        now: Current time; defaults to the clock. Tests pass a fixed value.

    Returns:
        ``(start, end)`` as aware UTC datetimes.

    Raises:
        ToolError: ``WINDOW_TOO_LARGE`` when the window is longer than
            ``max_hours``.
    """
    now = now or datetime.now(timezone.utc)

    if start or end:
        window_end = parse_time(end) if end else now
        window_start = parse_time(start) if start else window_end - timedelta(hours=DEFAULT_HOURS)
    else:
        window_end = now
        window_start = now - timedelta(hours=hours or DEFAULT_HOURS)

    if window_end - window_start > timedelta(hours=max_hours):
        raise ToolError(
            ErrorCode.WINDOW_TOO_LARGE,
            f"The window is longer than {max_hours:g} hours. Use a shorter range, "
            "or pass shipment_id to cover a whole shipment.",
        )
    return window_start, window_end


def shipment_period(shipment: Doc, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Returns the period of a shipment: departure to delivery, or to now.

    A shipment still in transit has no ``deliveredAt``, so its period ends
    now. This period is not capped by ``MAX_WINDOW_HOURS``: cumulative
    measures such as ``cumulativeMinutes`` are defined over the whole
    shipment.

    Args:
        shipment: A ``shipments`` document with ``departedAt`` and, once
            delivered, ``deliveredAt``.
        now: Current time; defaults to the clock.

    Returns:
        ``(start, end)`` as aware UTC datetimes.
    """
    end = shipment.get("deliveredAt") or now or datetime.now(timezone.utc)
    return shipment["departedAt"], end


def resolve(
    fleet_id: str | None = None,
    reefer_id: str | None = None,
    shipment_id: str | None = None,
    hours: float | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[Doc, datetime, datetime]:
    """Resolves scope and window for a sensor tool call in one step.

    The arguments are the tool call's own parameters, passed through as the
    LLM sent them. With a ``shipment_id`` the window is the shipment period
    and ``hours`` / ``start`` / ``end`` are ignored; otherwise :func:`window`
    applies with ``MAX_WINDOW_HOURS`` from the settings.

    Args:
        fleet_id: Fleet scope.
        reefer_id: Reefer scope.
        shipment_id: Shipment scope; switches the window to the shipment
            period.
        hours: Window length ending now (fleet or reefer scope).
        start: Window start, ISO 8601 (fleet or reefer scope).
        end: Window end, ISO 8601 (fleet or reefer scope).
        now: Current time; defaults to the clock.

    Returns:
        ``(scope, start, end)``: the readings filter from :func:`meta_filter`
        and the window as aware UTC datetimes, ready to pass to a detector.

    Raises:
        ToolError: ``SCOPE_REQUIRED`` without any id, ``NOT_FOUND`` for an
            unknown shipment, ``WINDOW_TOO_LARGE`` for a window over the
            limit.
    """
    scope = meta_filter(fleet_id, reefer_id, shipment_id)

    if shipment_id:
        shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": shipment_id})
        if shipment is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"No shipment with id {shipment_id}.")
        return scope, *shipment_period(shipment, now)

    return scope, *window(hours, start, end, max_hours=get_settings().max_window_hours, now=now)