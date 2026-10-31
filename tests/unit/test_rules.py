"""Unit tests for coldchain_copilot.rules.

The last tests feed the real clauses with events shaped like what detection
returns for the seeded scenarios, and check the statuses in
design/ontology.md §6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coldchain_copilot.catalog import Catalog
from coldchain_copilot.rules import (
    check_conditions,
    deadline,
    evaluate_clauses,
    evaluate_trigger,
)

T0 = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)

ART6 = {
    "clauseId": "art6",
    "chunkId": "art6-chunk",
    "kind": "obligation",
    "trigger": {
        "eventType": "event:temperature_excursion",
        "allOf": [
            {"measure": "direction", "op": "eq", "value": "above"},
            {"measure": "consecutiveMinutes", "op": "gte", "value": 15},
        ],
    },
    "action": {"type": "notify", "party": "shipper.qa"},
    "deadline": {"anchor": "eventStart", "offsetMinutes": 60},
}
ART7 = {
    "clauseId": "art7",
    "chunkId": "art7-chunk",
    "kind": "obligation",
    "trigger": {"clauseTriggered": "art6"},
    "action": {"type": "inspect", "party": "shipper.qa"},
}


def excursion(minutes: int, direction: str = "above", start: datetime = T0) -> dict:
    """Builds a temperature excursion event as detection returns it."""
    return {
        "eventType": "event:temperature_excursion",
        "reeferId": "RF-02",
        "start": start,
        "end": start + timedelta(minutes=minutes - 1),
        "measures": {"consecutiveMinutes": minutes, "direction": direction},
    }


# ------------------------------------------------------------------ conditions


def test_check_conditions_reports_each_comparison() -> None:
    basis = check_conditions(ART6["trigger"]["allOf"], {"direction": "above", "consecutiveMinutes": 24})
    assert basis == [
        {"measure": "direction", "op": "eq", "required": "above", "actual": "above", "met": True},
        {"measure": "consecutiveMinutes", "op": "gte", "required": 15, "actual": 24, "met": True},
    ]


@pytest.mark.parametrize(
    ("op", "actual", "value", "expected"),
    [
        ("gte", 15, 15, True),
        ("gte", 14, 15, False),
        ("gt", 15, 15, False),
        ("lt", 89, 90, True),
        ("lte", 90, 90, True),
        ("eq", "above", "above", True),
    ],
)
def test_operators(op: str, actual, value, expected: bool) -> None:
    [b] = check_conditions([{"measure": "m", "op": op, "value": value}], {"m": actual})
    assert b["met"] is expected


def test_missing_measure_is_none_not_false() -> None:
    [b] = check_conditions([{"measure": "m", "op": "gte", "value": 1}], {})
    assert b["actual"] is None and b["met"] is None


# ------------------------------------------------------------------- deadlines


def test_deadline_from_event_start() -> None:
    assert deadline(ART6, excursion(24)) == T0 + timedelta(minutes=60)


def test_deadline_from_event_end() -> None:
    clause = {**ART6, "deadline": {"anchor": "eventEnd", "offsetMinutes": 1440}}
    assert deadline(clause, excursion(24)) == T0 + timedelta(minutes=23 + 1440)


def test_no_deadline() -> None:
    assert deadline(ART7, excursion(24)) is None


# ------------------------------------------------------------- single trigger


def test_triggered_reports_earliest_matching_event() -> None:
    later = excursion(30, start=T0 + timedelta(hours=5))
    result = evaluate_trigger(ART6, [excursion(5), excursion(24, start=T0 + timedelta(hours=1)), later], True)

    assert result["status"] == "triggered"
    assert result["event"]["start"] == T0 + timedelta(hours=1)
    assert result["deadline"] == T0 + timedelta(hours=2)
    assert result["chunkId"] == "art6-chunk"


def test_not_triggered_reports_closest_event() -> None:
    result = evaluate_trigger(ART6, [excursion(5, "below"), excursion(10)], True)

    assert result["status"] == "not_triggered"
    assert result["deadline"] is None
    assert [b["actual"] for b in result["basis"]] == ["above", 10]


def test_no_event_with_data_is_not_triggered() -> None:
    result = evaluate_trigger(ART6, [], data_collected=True)
    assert result["status"] == "not_triggered"
    assert result["basis"] == [{"noEvent": "event:temperature_excursion"}]


def test_no_data_is_unresolved_never_not_triggered() -> None:
    result = evaluate_trigger(ART6, [], data_collected=False)
    assert result["status"] == "unresolved"


def test_missing_measure_is_unresolved() -> None:
    event = excursion(24)
    del event["measures"]["direction"]
    result = evaluate_trigger(ART6, [event], True)

    assert result["status"] == "unresolved"
    assert result["basis"] == [{"missingMeasure": "direction"}]


# ------------------------------------------------------------------- chaining


@pytest.mark.parametrize(
    ("events", "collected", "expected"),
    [([excursion(24)], True, "triggered"), ([excursion(5)], True, "not_triggered"), ([], False, "unresolved")],
)
def test_chained_clause_copies_parent_status(events, collected, expected) -> None:
    results = evaluate_clauses(
        [ART7, ART6],  # order of input must not matter
        {"event:temperature_excursion": events},
        {"event:temperature_excursion": collected},
    )
    art7, art6 = results
    assert art6["status"] == expected
    assert art7["status"] == expected
    assert art7["basis"] == [{"dependsOn": "art6"}]


def test_chain_of_chains() -> None:
    art9 = {**ART7, "clauseId": "art9", "chunkId": "art9-chunk", "trigger": {"clauseTriggered": "art7"}}
    results = evaluate_clauses(
        [art9, ART7, ART6],
        {"event:temperature_excursion": [excursion(24)]},
        {"event:temperature_excursion": True},
    )
    assert [r["status"] for r in results] == ["triggered", "triggered", "triggered"]


def test_dependency_not_selected_is_unresolved() -> None:
    [result] = evaluate_clauses([ART7], {}, {})
    assert result["status"] == "unresolved"


# ------------------------------------------------------- seeded scenarios


def clauses_for(catalog: Catalog, contract_id: str) -> list[dict]:
    """The shipment's contract clauses plus the seafood-wide recommendations."""
    return [c for c in catalog.clauses if c["contractId"] in (contract_id, None)]


def run_event(event_type: str, minutes: int, **measures) -> dict:
    return {
        "eventType": event_type,
        "reeferId": "RF",
        "start": T0,
        "end": T0 + timedelta(minutes=minutes - 1),
        "measures": measures,
    }


ALL_COLLECTED = {
    "event:temperature_excursion": True,
    "event:door_opening": True,
    "event:compressor_stop": True,
    "event:power_anomaly": True,
    "event:humidity_breach": False,  # no humidity sensor on any reefer
}


def statuses(results: list[dict]) -> dict[str, str]:
    return {r["clauseId"]: r["status"] for r in results}


def test_shp102_compressor_stop_scenario(catalog: Catalog) -> None:
    events = {
        "event:temperature_excursion": [
            run_event("event:temperature_excursion", 24, consecutiveMinutes=24, cumulativeMinutes=24,
                      direction="above", peakTemp=8.4, lowestTemp=4.1, cause="cause:compressor_stop")
        ],
        "event:compressor_stop": [run_event("event:compressor_stop", 30, stopMinutes=30)],
        "event:power_anomaly": [run_event("event:power_anomaly", 1440, powerChangePct=-1.8)],
    }
    result = statuses(evaluate_clauses(clauses_for(catalog, "blueharbor-live-fish"), events, ALL_COLLECTED))

    assert result == {
        "blueharbor-live-fish-v1-art6": "triggered",
        "blueharbor-live-fish-v1-art7": "triggered",
        "blueharbor-live-fish-v1-art8": "not_triggered",  # 24 cumulative < 30
        "sop-excursion-response-v1-sec2": "triggered",
        "sop-excursion-response-v1-sec3": "not_triggered",
        "sop-excursion-response-v1-sec4": "triggered",
        "manual-reefer-maintenance-v1-sec2": "not_triggered",
        "manual-reefer-maintenance-v1-sec3": "not_triggered",
        "manual-reefer-maintenance-v1-sec4": "triggered",
    }


def test_shp105_humidity_clause_unresolved(catalog: Catalog) -> None:
    events = {"event:power_anomaly": [run_event("event:power_anomaly", 1440, powerChangePct=0.0)]}
    result = statuses(evaluate_clauses(clauses_for(catalog, "tidalpearl-oyster"), events, ALL_COLLECTED))

    assert result["tidalpearl-oyster-v1-art5"] == "unresolved"
    assert result["tidalpearl-oyster-v1-art6"] == "not_triggered"
    assert "triggered" not in result.values()