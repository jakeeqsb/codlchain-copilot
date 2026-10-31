"""Unit tests for coldchain_copilot.detection.

These check that each detect rule becomes the pipeline described in the
module docstring, using the real rules from data/ontology.json. Whether the
pipelines return the right events is checked against seeded data in
tests/integration/test_detection_db.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coldchain_copilot.catalog import Catalog
from coldchain_copilot.detection import (
    RUN_GAP_MINUTES,
    attach_causes,
    run_pipeline,
    trend_pipeline,
)

SCOPE = {"meta.shipmentId": "SHP-102"}
END = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
START = END - timedelta(hours=72)


@pytest.fixture
def rules(catalog: Catalog) -> dict[str, dict]:
    """Returns each event class's detect rule, keyed by class id."""
    return {d["_id"]: d["detect"] for d in catalog.ontology if "detect" in d}


def stage(pipeline: list[dict], name: str, nth: int = 0) -> dict:
    """Returns the body of the nth stage called ``name``."""
    return [s[name] for s in pipeline if name in s][nth]


# ------------------------------------------------------------------------- run


def test_run_starts_with_scope_window_then_rule_match(rules: dict) -> None:
    rule = rules["event:door_opening"]
    pipeline = run_pipeline(rule, SCOPE, START, END)

    assert pipeline[0] == {"$match": {**SCOPE, "ts": {"$gte": START, "$lt": END}}}
    assert pipeline[1] == {"$match": {"doorOpen": True}}


def test_run_uses_shift_and_gap_rule(rules: dict) -> None:
    pipeline = run_pipeline(rules["event:door_opening"], SCOPE, START, END)

    shift = stage(pipeline, "$setWindowFields", 0)
    assert shift["output"]["prevTs"] == {"$shift": {"output": "$ts", "by": -1}}
    assert shift["sortBy"] == {"ts": 1}
    assert str(RUN_GAP_MINUTES) in str(stage(pipeline, "$set", 0))


def test_run_groups_with_summary_accumulators(rules: dict) -> None:
    rule = rules["event:temperature_excursion"]
    group = stage(run_pipeline(rule, SCOPE, START, END), "$group")

    assert group["start"] == {"$min": "$ts"}
    assert group["end"] == {"$max": "$ts"}
    for name, accumulator in rule["summary"].items():
        assert group[name] == accumulator


def test_run_sorts_before_group_so_first_is_earliest(rules: dict) -> None:
    pipeline = run_pipeline(rules["event:temperature_excursion"], SCOPE, START, END)
    names = [next(iter(s)) for s in pipeline]

    assert names[names.index("$group") - 1] == "$sort"


def test_run_measures_use_rule_names(rules: dict) -> None:
    rule = rules["event:temperature_excursion"]
    project = stage(run_pipeline(rule, SCOPE, START, END), "$project")

    assert set(project["measures"]) == {
        "consecutiveMinutes",
        "cumulativeMinutes",
        "peakTemp",
        "lowestTemp",
        "direction",
    }


def test_cumulative_only_when_rule_asks(rules: dict) -> None:
    with_cumulative = run_pipeline(rules["event:temperature_excursion"], SCOPE, START, END)
    without = run_pipeline(rules["event:door_opening"], SCOPE, START, END)

    assert "cumulativeMinutes" in str(with_cumulative)
    assert len([s for s in without if "$setWindowFields" in s]) == 2  # shift + run numbering only
    assert set(stage(without, "$project")["measures"]) == {"doorOpenMinutes"}


# ----------------------------------------------------------------------- trend


def test_trend_anchors_windows_on_latest_reading(rules: dict) -> None:
    rule = rules["event:power_anomaly"]  # recent 24 h, baseline 48 h
    pipeline = trend_pipeline(rule, SCOPE, START, END)

    assert pipeline[0] == {"$match": {**SCOPE, "ts": {"$gte": START, "$lt": END}}}
    last = stage(pipeline, "$setWindowFields")["output"]["lastTs"]
    assert last["$max"] == "$ts"
    assert stage(pipeline, "$set", 0)["recentStart"]["$dateSubtract"] == {
        "startDate": "$lastTs", "unit": "hour", "amount": 24
    }
    assert stage(pipeline, "$set", 1)["baselineStart"]["$dateSubtract"] == {
        "startDate": "$recentStart", "unit": "hour", "amount": 48
    }


def test_trend_averages_each_window(rules: dict) -> None:
    group = stage(trend_pipeline(rules["event:power_anomaly"], SCOPE, START, END), "$group")

    def avg(lo: str, hi: str) -> dict:
        in_window = {"$and": [{"$gt": ["$ts", lo]}, {"$lte": ["$ts", hi]}]}
        return {"$avg": {"$cond": [in_window, "$powerKw", None]}}

    assert group["recentAvg"] == avg("$recentStart", "$lastTs")
    assert group["baselineAvg"] == avg("$baselineStart", "$recentStart")


def test_trend_reports_change_under_rule_name(rules: dict) -> None:
    project = stage(trend_pipeline(rules["event:power_anomaly"], SCOPE, START, END), "$project")
    assert "powerChangePct" in project["measures"]


# ---------------------------------------------------------------------- causes


def event(shipment: str, start_min: int, end_min: int, **measures) -> dict:
    """Builds an event spanning minutes [start_min, end_min] after START."""
    return {
        "shipmentId": shipment,
        "start": START + timedelta(minutes=start_min),
        "end": START + timedelta(minutes=end_min),
        "measures": dict(measures),
    }


def test_overlapping_candidate_is_the_cause() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes(
        [excursion],
        {"event:compressor_stop": [event("SHP-102", 95, 124)], "event:door_opening": []},
    )
    assert excursion["measures"]["cause"] == "cause:compressor_stop"


def test_no_overlap_is_unknown() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes([excursion], {"event:compressor_stop": [event("SHP-102", 10, 40)]})
    assert excursion["measures"]["cause"] == "cause:unknown"


def test_other_shipment_does_not_count() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes([excursion], {"event:door_opening": [event("SHP-103", 100, 115)]})
    assert excursion["measures"]["cause"] == "cause:unknown"


def test_cause_order_follows_cause_from() -> None:
    excursion = event("SHP-102", 100, 123)
    attach_causes(
        [excursion],
        {
            "event:compressor_stop": [event("SHP-102", 95, 124)],
            "event:door_opening": [event("SHP-102", 101, 110)],
        },
    )
    assert excursion["measures"]["cause"] == "cause:compressor_stop"