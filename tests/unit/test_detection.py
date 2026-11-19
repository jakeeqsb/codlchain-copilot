"""Unit tests for coldchain_copilot.detection.

These check the pipeline shapes the two helpers build. Whether the detectors
return the right events is checked against seeded data in
tests/integration/test_detection_db.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from coldchain_copilot.detection import (
    ABOVE_MAX,
    BELOW_MIN,
    RUN_GAP_MINUTES,
    attach_causes,
    run_pipeline,
    trend_pipeline,
)

SCOPE = {"meta.shipmentId": "SHP-102"}
END = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
START = END - timedelta(hours=72)

EXCURSION = dict(
    match={"$expr": {"$or": [ABOVE_MAX, BELOW_MIN]}},
    duration_as="consecutiveMinutes",
    cumulative_as="cumulativeMinutes",
    add_fields={"side": {"$cond": [ABOVE_MAX, "above", "below"]}},
    summary={"peakTemp": {"$max": "$temp"}, "direction": {"$first": "$side"}},
)
DOOR = dict(match={"doorOpen": True}, duration_as="doorOpenMinutes")


def stage(pipeline: list[dict], name: str, nth: int = 0) -> dict:
    """Returns the body of the nth stage called ``name``."""
    return [s[name] for s in pipeline if name in s][nth]


def names(pipeline: list[dict]) -> list[str]:
    return [next(iter(s)) for s in pipeline]


# ------------------------------------------------------------------------- run


def test_run_starts_with_scope_window_then_match() -> None:
    pipeline = run_pipeline(SCOPE, START, END, **DOOR)

    assert pipeline[0] == {"$match": {**SCOPE, "ts": {"$gte": START, "$lt": END}}}
    assert pipeline[1] == {"$match": {"doorOpen": True}}


def test_run_adds_fields_only_when_asked() -> None:
    with_side = run_pipeline(SCOPE, START, END, **EXCURSION)
    without = run_pipeline(SCOPE, START, END, **DOOR)

    assert with_side[2] == {"$set": EXCURSION["add_fields"]}
    assert "$setWindowFields" in without[2]


def test_run_uses_shift_and_gap_rule() -> None:
    pipeline = run_pipeline(SCOPE, START, END, **DOOR)

    shift = stage(pipeline, "$setWindowFields", 0)
    assert shift["output"]["prevTs"] == {"$shift": {"output": "$ts", "by": -1}}
    assert shift["sortBy"] == {"ts": 1}
    assert str(RUN_GAP_MINUTES) in str(stage(pipeline, "$set", 0))


def test_run_groups_with_summary_after_sort() -> None:
    pipeline = run_pipeline(SCOPE, START, END, **EXCURSION)
    group = stage(pipeline, "$group")

    assert group["start"] == {"$min": "$ts"}
    assert group["end"] == {"$max": "$ts"}
    assert group["peakTemp"] == {"$max": "$temp"}
    assert group["direction"] == {"$first": "$side"}
    order = names(pipeline)
    assert order[order.index("$group") - 1] == "$sort"  # makes $first the earliest reading


def test_run_measures_and_cumulative() -> None:
    with_cumulative = run_pipeline(SCOPE, START, END, **EXCURSION)
    without = run_pipeline(SCOPE, START, END, **DOOR)

    assert set(stage(with_cumulative, "$project")["measures"]) == {
        "consecutiveMinutes", "cumulativeMinutes", "peakTemp", "direction",
    }
    assert set(stage(without, "$project")["measures"]) == {"doorOpenMinutes"}
    assert names(without).count("$setWindowFields") == 2  # shift + run numbering only


# ----------------------------------------------------------------------- trend


POWER = dict(field="powerKw", recent_hours=24, baseline_hours=48, change_as="powerChangePct")


def test_trend_anchors_windows_on_latest_reading() -> None:
    pipeline = trend_pipeline(SCOPE, START, END, **POWER)

    assert pipeline[0] == {"$match": {**SCOPE, "ts": {"$gte": START, "$lt": END}}}
    assert stage(pipeline, "$setWindowFields")["output"]["lastTs"]["$max"] == "$ts"
    assert stage(pipeline, "$set", 0)["recentStart"]["$dateSubtract"] == {
        "startDate": "$lastTs", "unit": "hour", "amount": 24,
    }
    assert stage(pipeline, "$set", 1)["baselineStart"]["$dateSubtract"] == {
        "startDate": "$recentStart", "unit": "hour", "amount": 48,
    }


def test_trend_averages_each_window() -> None:
    group = stage(trend_pipeline(SCOPE, START, END, **POWER), "$group")

    def avg(lo: str, hi: str) -> dict:
        in_window = {"$and": [{"$gt": ["$ts", lo]}, {"$lte": ["$ts", hi]}]}
        return {"$avg": {"$cond": [in_window, "$powerKw", None]}}

    assert group["recentAvg"] == avg("$recentStart", "$lastTs")
    assert group["baselineAvg"] == avg("$baselineStart", "$recentStart")


def test_trend_reports_change_under_given_name() -> None:
    project = stage(trend_pipeline(SCOPE, START, END, **POWER), "$project")
    assert "powerChangePct" in project["measures"]


# ---------------------------------------------------------------------- causes


def event(shipment: str, start_min: int, end_min: int) -> dict:
    """Builds an event spanning minutes [start_min, end_min] after START."""
    return {
        "shipmentId": shipment,
        "start": START + timedelta(minutes=start_min),
        "end": START + timedelta(minutes=end_min),
        "measures": {},
    }


def test_overlapping_candidate_is_the_cause() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes([excursion], {"event:compressor_stop": [event("SHP-102", 95, 124)], "event:door_opening": []})
    assert excursion["measures"]["cause"] == "cause:compressor_stop"


def test_no_overlap_is_unknown() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes([excursion], {"event:compressor_stop": [event("SHP-102", 10, 40)]})
    assert excursion["measures"]["cause"] == "cause:unknown"


def test_other_shipment_does_not_count() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes([excursion], {"event:door_opening": [event("SHP-103", 100, 115)]})
    assert excursion["measures"]["cause"] == "cause:unknown"


def test_cause_order_is_priority() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes(
        [excursion],
        {"event:compressor_stop": [event("SHP-102", 95, 124)], "event:door_opening": [event("SHP-102", 101, 110)]},
    )
    assert excursion["measures"]["cause"] == "cause:compressor_stop"