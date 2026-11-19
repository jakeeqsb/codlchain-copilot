"""Detector functions: compute events from ``readings`` with MongoDB aggregation.

Each event type in the ontology names its detector, for example::

    {"_id": "event:temperature_excursion", "kind": "eventType",
     "detector": "detect_temperature_excursion", "measures": [...]}

and ``registry.py`` maps that name to the function of the same name here. The
ontology says *what* the event is and which measures it produces; the
function says *how* to find it, and the MongoDB pipeline is right there in
the code.

Events are not stored. Every call aggregates the Time Series collection, so a
corrected reading shows up on the next call without any backfill.

Two shared helpers hold the actual pipeline shapes:

``run_pipeline``
    Consecutive readings that match a condition form one event ("door open",
    "temperature outside the policy range"). Two matching readings belong to
    the same run when they are at most ``RUN_GAP_MINUTES`` apart, so one
    missed reading does not split an excursion in two.
``trend_pipeline``
    The average of a field over the most recent hours compared with the
    hours just before, for example power drawn in the last 24 hours against
    the 48 hours before that.

Every detector takes the same arguments and returns events of the same shape::

    detect_x(scope, start, end) -> [
        {"eventType": "event:...", "shipmentId": "SHP-102", "reeferId": "RF-02",
         "start": datetime, "end": datetime,
         "measures": {"consecutiveMinutes": 24, ...}},
    ]

``scope`` filters reading metadata, e.g. ``{"meta.shipmentId": "SHP-102"}``.
The window is ``[start, end)``. The keys in ``measures`` are the measure
names listed on the event type and used by clause conditions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import db

Doc = dict[str, Any]

RUN_GAP_MINUTES = 2
"""Largest gap between two matching readings that still counts as one run."""

ABOVE_MAX = {"$gt": ["$temp", "$meta.temperaturePolicy.maxC"]}
BELOW_MIN = {"$lt": ["$temp", "$meta.temperaturePolicy.minC"]}


# ------------------------------------------------------------------- detectors


def detect_temperature_excursion(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Temperature outside the shipment's policy range, as consecutive runs.

    The policy comes from the snapshot stored in each reading's ``meta``, so
    the comparison is a field-to-field ``$expr``. Measures: run length
    (``consecutiveMinutes``), the shipment's total (``cumulativeMinutes``),
    ``peakTemp``, ``lowestTemp``, ``direction`` of the first reading
    (``above``/``below``), and ``cause``: a compressor stop or door opening
    whose run overlaps the excursion, checked in that order.
    """
    events = _run_events(
        "event:temperature_excursion",
        scope,
        start,
        end,
        match={"$expr": {"$or": [ABOVE_MAX, BELOW_MIN]}},
        duration_as="consecutiveMinutes",
        cumulative_as="cumulativeMinutes",
        add_fields={"side": {"$cond": [ABOVE_MAX, "above", "below"]}},
        summary={
            "peakTemp": {"$max": "$temp"},
            "lowestTemp": {"$min": "$temp"},
            "direction": {"$first": "$side"},
        },
    )
    if events:
        attach_causes(
            events,
            {
                "event:compressor_stop": detect_compressor_stop(scope, start, end),
                "event:door_opening": detect_door_opening(scope, start, end),
            },
        )
    return events


def detect_door_opening(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Door open, as consecutive runs. Measure: ``doorOpenMinutes``."""
    return _run_events(
        "event:door_opening", scope, start, end,
        match={"doorOpen": True},
        duration_as="doorOpenMinutes",
    )


def detect_compressor_stop(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Compressor drawing almost no power (< 0.1 kW). Measure: ``stopMinutes``."""
    return _run_events(
        "event:compressor_stop", scope, start, end,
        match={"powerKw": {"$lt": 0.1}},
        duration_as="stopMinutes",
    )


def detect_humidity_breach(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Relative humidity below 90 %. Measures: ``humidityMinutes``, ``minHumidityPct``.

    No seeded reefer has a humidity sensor, so this returns nothing; the
    engine uses ``field_collected`` to report the clause as unresolved.
    """
    return _run_events(
        "event:humidity_breach", scope, start, end,
        match={"humidityPct": {"$lt": 90}},
        duration_as="humidityMinutes",
        summary={"minHumidityPct": {"$min": "$humidityPct"}},
    )


def detect_power_anomaly(scope: Doc, start: datetime, end: datetime) -> list[Doc]:
    """Average power in the last 24 h against the 48 h before. Measure: ``powerChangePct``."""
    return _trend_event(
        "event:power_anomaly", scope, start, end,
        field="powerKw", recent_hours=24, baseline_hours=48, change_as="powerChangePct",
    )


# --------------------------------------------------------------------- helpers


def _run_events(event_type: str, scope: Doc, start: datetime, end: datetime, **spec: Any) -> list[Doc]:
    """Runs :func:`run_pipeline` and tags each result with the event type."""
    pipeline = run_pipeline(scope, start, end, **spec)
    return [{"eventType": event_type, **doc} for doc in db.collection(db.READINGS).aggregate(pipeline)]


def _trend_event(event_type: str, scope: Doc, start: datetime, end: datetime, **spec: Any) -> list[Doc]:
    """Runs :func:`trend_pipeline` and tags each result with the event type."""
    pipeline = trend_pipeline(scope, start, end, **spec)
    return [{"eventType": event_type, **doc} for doc in db.collection(db.READINGS).aggregate(pipeline)]


def run_pipeline(
    scope: Doc,
    start: datetime,
    end: datetime,
    *,
    match: Doc,
    duration_as: str,
    cumulative_as: str | None = None,
    add_fields: Doc | None = None,
    summary: Doc | None = None,
) -> list[Doc]:
    """Builds the aggregation that turns matching readings into runs.

    The stages, in order:

    1. Keep readings in the scope and window, then those that satisfy
       ``match``. Scope and time come first so the ``{meta.*, ts}`` indexes
       apply; ``match`` is separate because it may be an ``$expr``.
       ``add_fields`` adds per-reading values the summary needs (``side``).
    2. Per shipment, in time order, ``$shift`` gives each reading the
       previous matching reading's time. A gap over ``RUN_GAP_MINUTES``
       (or no previous reading) starts a new run.
    3. A running ``$sum`` of those "new run" flags numbers the runs.
    4. ``$group`` turns each run into one event: first and last timestamp
       plus the ``summary`` accumulators. The ``$sort`` before it makes
       ``$first`` mean "earliest reading".
    5. Duration is ``last - first + 1`` minutes (one reading per minute, so a
       single matching reading is a one-minute run), stored as ``duration_as``.
    6. With ``cumulative_as``, each event also gets the sum of all its
       shipment's run durations in the window.
    """
    summary = summary or {}
    pipeline: list[Doc] = [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {"$match": match},
        *([{"$set": add_fields}] if add_fields else []),
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "sortBy": {"ts": 1},
                "output": {"prevTs": {"$shift": {"output": "$ts", "by": -1}}},
            }
        },
        {
            "$set": {
                "newRun": {
                    "$cond": [
                        {
                            "$or": [
                                {"$eq": ["$prevTs", None]},
                                {
                                    "$gt": [
                                        {"$dateDiff": {"startDate": "$prevTs", "endDate": "$ts", "unit": "minute"}},
                                        RUN_GAP_MINUTES,
                                    ]
                                },
                            ]
                        },
                        1,
                        0,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "sortBy": {"ts": 1},
                "output": {"runNo": {"$sum": "$newRun", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {"$sort": {"ts": 1}},
        {
            "$group": {
                "_id": {"shipmentId": "$meta.shipmentId", "reeferId": "$meta.reeferId", "runNo": "$runNo"},
                "start": {"$min": "$ts"},
                "end": {"$max": "$ts"},
                **summary,
            }
        },
        {
            "$set": {
                duration_as: {"$add": [{"$dateDiff": {"startDate": "$start", "endDate": "$end", "unit": "minute"}}, 1]}
            }
        },
    ]

    if cumulative_as:
        pipeline.append(
            {
                "$setWindowFields": {
                    "partitionBy": "$_id.shipmentId",
                    "output": {
                        cumulative_as: {"$sum": f"${duration_as}", "window": {"documents": ["unbounded", "unbounded"]}}
                    },
                }
            }
        )

    measures = [duration_as, *summary, *([cumulative_as] if cumulative_as else [])]
    pipeline += [
        {
            "$project": {
                "_id": 0,
                "shipmentId": "$_id.shipmentId",
                "reeferId": "$_id.reeferId",
                "start": 1,
                "end": 1,
                "measures": {name: f"${name}" for name in measures},
            }
        },
        {"$sort": {"start": 1, "reeferId": 1}},
    ]
    return pipeline


def trend_pipeline(
    scope: Doc,
    start: datetime,
    end: datetime,
    *,
    field: str,
    recent_hours: int,
    baseline_hours: int,
    change_as: str,
) -> list[Doc]:
    """Builds the aggregation that compares a recent average with a baseline.

    The windows are anchored on each shipment's latest reading inside
    ``[start, end)``, not on ``end``. "Recent" is the last ``recent_hours``
    of data the reefer actually sent; the baseline is the ``baseline_hours``
    before that. Anchoring on the clock would mix in hours without data when
    readings stop early (a quiet sensor, or seed data queried the next day).

    One ``$group`` per shipment averages ``field`` twice; ``$cond`` passes the
    value only inside each window and ``null`` otherwise, which ``$avg``
    ignores. The result is the percentage change, rounded to one decimal.
    Every shipment with data in both windows gets one event, even for a tiny
    change, so a clause can report the actual value when it is not met.
    """
    value = f"${field}"

    def avg_between(lo: str, hi: str) -> Doc:
        # Window (lo, hi]: excludes its start, includes its end (the latest reading).
        in_window = {"$and": [{"$gt": ["$ts", lo]}, {"$lte": ["$ts", hi]}]}
        return {"$avg": {"$cond": [in_window, value, None]}}

    return [
        {"$match": {**scope, "ts": {"$gte": start, "$lt": end}}},
        {
            "$setWindowFields": {
                "partitionBy": "$meta.shipmentId",
                "output": {"lastTs": {"$max": "$ts", "window": {"documents": ["unbounded", "unbounded"]}}},
            }
        },
        {"$set": {"recentStart": {"$dateSubtract": {"startDate": "$lastTs", "unit": "hour", "amount": recent_hours}}}},
        {"$set": {"baselineStart": {"$dateSubtract": {"startDate": "$recentStart", "unit": "hour", "amount": baseline_hours}}}},
        {
            "$group": {
                "_id": {"shipmentId": "$meta.shipmentId", "reeferId": "$meta.reeferId"},
                "start": {"$max": "$recentStart"},
                "end": {"$max": "$lastTs"},
                "recentAvg": avg_between("$recentStart", "$lastTs"),
                "baselineAvg": avg_between("$baselineStart", "$recentStart"),
            }
        },
        {"$match": {"recentAvg": {"$ne": None}, "baselineAvg": {"$gt": 0}}},
        {
            "$project": {
                "_id": 0,
                "shipmentId": "$_id.shipmentId",
                "reeferId": "$_id.reeferId",
                "start": 1,
                "end": 1,
                "measures": {
                    change_as: {
                        "$round": [{"$subtract": [{"$multiply": [{"$divide": ["$recentAvg", "$baselineAvg"]}, 100]}, 100]}, 1]
                    },
                    "recentAvg": {"$round": ["$recentAvg", 3]},
                    "baselineAvg": {"$round": ["$baselineAvg", 3]},
                },
            }
        },
        {"$sort": {"reeferId": 1}},
    ]


def attach_causes(events: list[Doc], candidates: dict[str, list[Doc]]) -> None:
    """Sets ``measures.cause`` on each event from overlapping candidate events.

    A candidate is a cause when it is on the same shipment and its time span
    overlaps the event's. Candidate types are tried in dict order, which is
    the priority when two overlap. ``event:compressor_stop`` becomes
    ``cause:compressor_stop``; no overlap gives ``cause:unknown``.

    Modifies ``events`` in place.
    """
    for event in events:
        event["measures"]["cause"] = "cause:unknown"
        for cause_type, found in candidates.items():
            if any(
                c["shipmentId"] == event["shipmentId"] and c["start"] <= event["end"] and c["end"] >= event["start"]
                for c in found
            ):
                event["measures"]["cause"] = "cause:" + cause_type.split(":", 1)[1]
                break


def field_collected(field: str, scope: Doc, start: datetime, end: datetime) -> bool:
    """Tells whether any reading in the scope and window has ``field``.

    Used with an event type's ``requiresFields``. If the field was never
    collected (no humidity sensor on RF-05), finding no event means
    "unknown", not "nothing happened", and the clause is ``unresolved``.
    """
    query = {**scope, "ts": {"$gte": start, "$lt": end}, field: {"$exists": True}}
    return db.collection(db.READINGS).find_one(query, {"_id": 1}) is not None