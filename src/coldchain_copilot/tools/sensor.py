"""Level 2 sensor tools: what the readings say, for a fleet, reefer, or shipment.

Four tools, all read-only:

- ``find_excursions``: temperature outside the policy range, as events.
- ``find_energy_anomalies``: power drawn recently against a baseline.
- ``get_fleet_overview``: one summary row per shipment in a fleet.
- ``get_reefer_timeline``: one reefer's readings in time buckets.

The first two call the detectors in ``detection.py`` and return their events
unchanged, so the LLM sees exactly what the engine judges. They add no
thresholds of their own: whether an event needs action is decided by the
clauses, through ``evaluate_obligations``.

Every tool first resolves scope and window with :func:`resolve`
(design/interfaces.md "Common Rules"):

- Scope is required. ``fleet_id``, ``reefer_id``, and ``shipment_id`` map to
  the reading's meta fields; a call with none of them is rejected, never
  widened to the whole fleet.
- A fleet or reefer scope takes ``hours`` (default 24, ending now) or an
  explicit ``start`` / ``end``, at most ``MAX_WINDOW_HOURS`` long.
- A shipment scope ignores those and uses the shipment period, so cumulative
  measures cover the whole shipment.

Results keep ``datetime`` values; ``tools/gateway.py`` serializes them.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from coldchain_copilot import db
from coldchain_copilot.config import get_settings
from coldchain_copilot.detection import detect_power_anomaly, detect_temperature_excursion
from coldchain_copilot.errors import ErrorCode, ToolError

Doc = dict[str, Any]

DEFAULT_HOURS = 24
"""Window length when a fleet or reefer call gives neither hours nor times."""

MAX_BUCKETS = 96
"""Most buckets ``get_reefer_timeline`` returns; the bucket widens to fit."""


# ------------------------------------------------------------------ the tools


def find_excursions(
    fleet_id: str | None = None,
    reefer_id: str | None = None,
    shipment_id: str | None = None,
    hours: float | None = None,
    start: str | None = None,
    end: str | None = None,
    min_minutes: int = 2,
) -> Doc:
    """Finds runs of temperature outside each shipment's policy range.

    Calls ``detect_temperature_excursion`` with the resolved scope and window
    and keeps runs of at least ``min_minutes``, so a single stray reading does
    not show up as an incident. The events are the detector's own: measures
    ``direction``, ``consecutiveMinutes``, ``cumulativeMinutes``, ``peakTemp``,
    ``lowestTemp``, and ``cause``. Cargo and the policy range are on the
    shipment (``get_shipment``).

    Args:
        fleet_id: Fleet scope.
        reefer_id: Reefer scope.
        shipment_id: Shipment scope; the window becomes the shipment period.
        hours: Window length ending now.
        start: Window start, ISO 8601.
        end: Window end, ISO 8601.
        min_minutes: Shortest run to report.

    Returns:
        ``{"scope", "window", "events"}``.
    """
    scope, window_start, window_end = resolve(fleet_id, reefer_id, shipment_id, hours, start, end)
    events = detect_temperature_excursion(scope, window_start, window_end)
    return {
        "scope": scope,
        "window": {"start": window_start, "end": window_end},
        "events": [e for e in events if e["measures"]["consecutiveMinutes"] >= min_minutes],
    }


def find_energy_anomalies(
    fleet_id: str | None = None,
    reefer_id: str | None = None,
    shipment_id: str | None = None,
) -> Doc:
    """Reports each shipment's recent power draw against its baseline.

    Calls ``detect_power_anomaly``: the last 24 hours of data against the 48
    hours before, anchored on each shipment's latest reading
    (``measures.powerChangePct``). The windows are part of the measure's
    definition, so this tool takes no window of its own; it looks at the
    longest window allowed (or the shipment period) to find those 72 hours.

    It reports the number only. Whether a change is large enough to act on is
    a clause condition.

    Args:
        fleet_id: Fleet scope.
        reefer_id: Reefer scope.
        shipment_id: Shipment scope.

    Returns:
        ``{"scope", "events"}`` with one event per shipment that has data in
        both windows.
    """
    hours = get_settings().max_window_hours
    scope, window_start, window_end = resolve(fleet_id, reefer_id, shipment_id, hours)
    return {"scope": scope, "events": detect_power_anomaly(scope, window_start, window_end)}


def get_fleet_overview(
    fleet_id: str,
    hours: float | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Doc:
    """Summarizes every shipment in a fleet over one window.

    One row per shipment: average, min, max, and last temperature, average
    power, door-open minutes, and the number of temperature excursions. The
    excursion count comes from ``detect_temperature_excursion``, so it always
    matches ``find_excursions`` for the same window.

    Args:
        fleet_id: Fleet to summarize.
        hours: Window length ending now.
        start: Window start, ISO 8601.
        end: Window end, ISO 8601.

    Returns:
        ``{"fleetId", "window", "shipments"}``.
    """
    scope, window_start, window_end = resolve(fleet_id=fleet_id, hours=hours, start=start, end=end)
    rows = list(db.collection(db.READINGS).aggregate(overview_pipeline(scope, window_start, window_end)))

    excursions = detect_temperature_excursion(scope, window_start, window_end)
    for row in rows:
        row["excursionCount"] = sum(e["shipmentId"] == row["shipmentId"] for e in excursions)

    return {"fleetId": fleet_id, "window": {"start": window_start, "end": window_end}, "shipments": rows}


def get_reefer_timeline(
    reefer_id: str,
    hours: float | None = None,
    start: str | None = None,
    end: str | None = None,
    bucket_minutes: int = 5,
) -> Doc:
    """Returns one reefer's readings averaged into time buckets.

    Per bucket: average, min, and max temperature, average and min power,
    and whether the door was open at any point. The bucket is widened when
    needed so the result has at most ``MAX_BUCKETS`` rows (72 hours at
    5 minutes would be 864).

    Args:
        reefer_id: Reefer to chart.
        hours: Window length ending now.
        start: Window start, ISO 8601.
        end: Window end, ISO 8601.
        bucket_minutes: Requested bucket size.

    Returns:
        ``{"reeferId", "window", "bucketMinutes", "buckets"}``.
    """
    scope, window_start, window_end = resolve(reefer_id=reefer_id, hours=hours, start=start, end=end)
    bucket = bucket_size(window_start, window_end, bucket_minutes)
    buckets = list(db.collection(db.READINGS).aggregate(timeline_pipeline(scope, window_start, window_end, bucket)))
    return {
        "reeferId": reefer_id,
        "window": {"start": window_start, "end": window_end},
        "bucketMinutes": bucket,
        "buckets": buckets,
    }


# ------------------------------------------------------------------ pipelines


def overview_pipeline(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Builds the per-shipment summary aggregation for ``get_fleet_overview``.

    Readings are sorted by time before ``$group`` so ``$last`` is the most
    recent temperature. There is one reading per minute, so counting
    door-open readings gives door-open minutes.
    """
    return [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {"$sort": {"ts": 1}},
        {
            "$group": {
                "_id": {"shipmentId": "$meta.shipmentId", "reeferId": "$meta.reeferId"},
                "avgTemp": {"$avg": "$temp"},
                "minTemp": {"$min": "$temp"},
                "maxTemp": {"$max": "$temp"},
                "lastTemp": {"$last": "$temp"},
                "avgPowerKw": {"$avg": "$powerKw"},
                "doorOpenMinutes": {"$sum": {"$cond": ["$doorOpen", 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "shipmentId": "$_id.shipmentId",
                "reeferId": "$_id.reeferId",
                "avgTemp": {"$round": ["$avgTemp", 1]},
                "minTemp": 1,
                "maxTemp": 1,
                "lastTemp": 1,
                "avgPowerKw": {"$round": ["$avgPowerKw", 2]},
                "doorOpenMinutes": 1,
            }
        },
        {"$sort": {"shipmentId": 1}},
    ]


def timeline_pipeline(scope: Doc, start: datetime, end: datetime, bucket_minutes: int) -> list[Doc]:
    """Builds the bucketed aggregation for ``get_reefer_timeline``.

    Buckets are counted from the window start: a reading ``n`` minutes after
    ``start`` falls in bucket ``n // bucket_minutes``, which starts at
    ``start + index * bucket_minutes``. ``$dateTrunc`` with ``binSize`` is not
    used because it aligns bins to a fixed reference time, not to ``start``,
    so a window that does not begin on a bin boundary gets one extra partial
    bucket at each end.
    """
    # Date minus date is milliseconds. Exact even when start has seconds, unlike
    # $dateDiff, which counts minute boundaries crossed.
    ms_in = {"$subtract": ["$ts", start]}
    index = {"$floor": {"$divide": [ms_in, bucket_minutes * 60_000]}}
    bucket_start = {"$dateAdd": {"startDate": start, "unit": "minute", "amount": {"$multiply": [index, bucket_minutes]}}}
    return [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {
            "$group": {
                "_id": bucket_start,
                "avgTemp": {"$avg": "$temp"},
                "minTemp": {"$min": "$temp"},
                "maxTemp": {"$max": "$temp"},
                "avgPowerKw": {"$avg": "$powerKw"},
                "minPowerKw": {"$min": "$powerKw"},
                "doorOpen": {"$max": "$doorOpen"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "ts": "$_id",
                "avgTemp": {"$round": ["$avgTemp", 1]},
                "minTemp": 1,
                "maxTemp": 1,
                "avgPowerKw": {"$round": ["$avgPowerKw", 2]},
                "minPowerKw": 1,
                "doorOpen": 1,
            }
        },
        {"$sort": {"ts": 1}},
    ]


def bucket_size(start: datetime, end: datetime, requested: int) -> int:
    """Returns the bucket in minutes: the requested size, widened to fit ``MAX_BUCKETS``."""
    window_minutes = (end - start).total_seconds() / 60
    return max(requested, math.ceil(window_minutes / MAX_BUCKETS))


# ---------------------------------------------------------- scope and window


def meta_filter(
    fleet_id: str | None = None,
    reefer_id: str | None = None,
    shipment_id: str | None = None,
) -> Doc:
    """Builds the readings filter for the given scope.

    Each id given becomes one condition on the reading's meta. Several ids
    narrow the scope together (a fleet and a reefer in it), combined with AND.

    Args:
        fleet_id: Matched against ``meta.fleetId``.
        reefer_id: Matched against ``meta.reeferId``.
        shipment_id: Matched against ``meta.shipmentId``.

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
    is read as UTC, the zone the database stores. The tool schemas ask the LLM
    for an offset, so this is a fallback, not the expected input.

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

    Explicit times win over ``hours``. When only one of ``start`` and ``end``
    is given, the other is filled in: a missing end is now, a missing start is
    ``DEFAULT_HOURS`` before the end. With no times at all, the window is the
    last ``hours`` (default 24) ending now.

    Args:
        hours: Length of a window that ends now.
        start: Window start, ISO 8601.
        end: Window end, ISO 8601.
        max_hours: Longest allowed window, normally ``MAX_WINDOW_HOURS``.
        now: Current time; defaults to the clock. Tests pass a fixed value.

    Returns:
        ``(start, end)`` as aware UTC datetimes.

    Raises:
        ToolError: ``WINDOW_TOO_LARGE`` when longer than ``max_hours``.
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
    """Returns a shipment's period: departure to delivery, or to now in transit.

    Not capped by ``MAX_WINDOW_HOURS``: cumulative measures such as
    ``cumulativeMinutes`` are defined over the whole shipment.
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
    """Resolves scope and window for a tool call in one step.

    With a ``shipment_id`` the window is the shipment period and ``hours`` /
    ``start`` / ``end`` are ignored; otherwise :func:`window` applies with
    ``MAX_WINDOW_HOURS`` from the settings.

    Returns:
        ``(scope, start, end)``, ready to pass to a detector.

    Raises:
        ToolError: ``SCOPE_REQUIRED`` without any id, ``NOT_FOUND`` for an
            unknown shipment, ``WINDOW_TOO_LARGE`` for a window over the limit.
    """
    scope = meta_filter(fleet_id, reefer_id, shipment_id)

    if shipment_id:
        shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": shipment_id})
        if shipment is None:
            raise ToolError(ErrorCode.NOT_FOUND, f"No shipment with id {shipment_id}.")
        return scope, *shipment_period(shipment, now)

    return scope, *window(hours, start, end, max_hours=get_settings().max_window_hours, now=now)