"""End-to-end engine runs against seeded data (needs MongoDB with scripts/seed.py loaded).

Each test evaluates one seeded shipment with ``engine.evaluate_shipment`` and
checks every clause's status against design/ontology.md §6, plus the
deadline, the referenced follow-up clause, and the briefing items.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from coldchain_copilot import engine
from coldchain_copilot.errors import ToolError

pytestmark = pytest.mark.integration

T, N, U = "triggered", "not_triggered", "unresolved"

RECOMMENDATIONS = [
    "manual-reefer-maintenance-v1-sec2",
    "manual-reefer-maintenance-v1-sec3",
    "manual-reefer-maintenance-v1-sec4",
    "sop-excursion-response-v1-sec2",
    "sop-excursion-response-v1-sec3",
    "sop-excursion-response-v1-sec4",
]

EXPECTED = {
    "SHP-101": {
        "blueharbor-live-fish-v1-art6": N, "blueharbor-live-fish-v1-art7": N, "blueharbor-live-fish-v1-art8": N,
        **dict.fromkeys(RECOMMENDATIONS, N),
    },
    "SHP-102": {
        "blueharbor-live-fish-v1-art6": T, "blueharbor-live-fish-v1-art7": T, "blueharbor-live-fish-v1-art8": N,
        "manual-reefer-maintenance-v1-sec2": N, "manual-reefer-maintenance-v1-sec3": N,
        "manual-reefer-maintenance-v1-sec4": T,
        "sop-excursion-response-v1-sec2": T, "sop-excursion-response-v1-sec3": N,
        "sop-excursion-response-v1-sec4": T,
    },
    "SHP-103": {
        "oceanpeak-tuna-v1-art5": T, "oceanpeak-tuna-v1-art6": T, "oceanpeak-tuna-v1-art7": T,
        "manual-reefer-maintenance-v1-sec2": N, "manual-reefer-maintenance-v1-sec3": N,
        "manual-reefer-maintenance-v1-sec4": N,
        "sop-excursion-response-v1-sec2": N, "sop-excursion-response-v1-sec3": T,
        "sop-excursion-response-v1-sec4": T,
    },
    "SHP-104": {
        "coastalfresh-shrimp-v1-art5": T,
        "manual-reefer-maintenance-v1-sec2": T, "manual-reefer-maintenance-v1-sec3": N,
        "manual-reefer-maintenance-v1-sec4": N,
        "sop-excursion-response-v1-sec2": N, "sop-excursion-response-v1-sec3": N,
        "sop-excursion-response-v1-sec4": N,
    },
    "SHP-105": {
        "tidalpearl-oyster-v1-art5": U, "tidalpearl-oyster-v1-art6": N,
        **dict.fromkeys(RECOMMENDATIONS, N),
    },
}


@pytest.mark.parametrize("shipment_id", sorted(EXPECTED))
def test_every_clause_status(mongo_db, shipment_id: str) -> None:
    report = engine.evaluate_shipment(shipment_id)
    assert {r["clauseId"]: r["status"] for r in report["results"]} == EXPECTED[shipment_id]


def test_shp102_art6_deadline_and_follow_up(mongo_db) -> None:
    report = engine.evaluate_shipment("SHP-102")
    art6 = next(r for r in report["results"] if r["clauseId"] == "blueharbor-live-fish-v1-art6")

    assert art6["deadline"] == art6["event"]["start"] + timedelta(minutes=60)
    assert art6["follows"] == ["blueharbor-live-fish-v1-art7"]  # via follow_references ($graphLookup)
    assert {"measure": "consecutiveMinutes", "op": "gte", "required": 15, "actual": 24, "met": True} in art6["basis"]


def test_shp102_briefing_items(mongo_db) -> None:
    actions = engine.evaluate_shipment("SHP-102")["actions"]
    by_clause = {a["clauseId"]: a for a in actions}

    assert set(by_clause) == {c for c, s in EXPECTED["SHP-102"].items() if s == T}
    assert by_clause["blueharbor-live-fish-v1-art6"]["action"] == "notify"
    assert by_clause["blueharbor-live-fish-v1-art6"]["party"] == "shipper.qa"
    assert by_clause["blueharbor-live-fish-v1-art6"]["evidence"] == "blueharbor-live-fish-v1-article-6"


def test_unknown_shipment(mongo_db) -> None:
    with pytest.raises(ToolError) as exc_info:
        engine.evaluate_shipment("SHP-999")
    assert exc_info.value.code == "NOT_FOUND"