"""Sensor tools against seeded data (needs MongoDB with scripts/seed.py loaded).

Fleet and reefer calls use an explicit window over the seeded history:
departure of the shipment to 72 hours later. Expected values match
tests/integration/test_detection_db.py, because the tools call the same
detectors.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from coldchain_copilot import db
from coldchain_copilot.errors import ToolError
from coldchain_copilot.tools import sensor

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def seeded(mongo_db):
    """Fleet id and an explicit 72-hour window covering the seeded readings."""
    shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": "SHP-102"})
    start = shipment["departedAt"]
    return {
        "fleet_id": shipment["fleetId"],
        "start": start.isoformat(),
        "end": (start + timedelta(hours=72)).isoformat(),
    }


def test_excursions_for_a_fleet(seeded) -> None:
    result = sensor.find_excursions(**seeded)
    by_shipment = {e["shipmentId"]: e for e in result["events"]}

    assert by_shipment["SHP-102"]["measures"]["consecutiveMinutes"] == 24
    assert by_shipment["SHP-102"]["measures"]["cause"] == "cause:compressor_stop"
    assert by_shipment["SHP-103"]["measures"]["cause"] == "cause:door_opening"


def test_excursions_for_a_shipment_use_its_period(mongo_db) -> None:
    [event] = sensor.find_excursions(shipment_id="SHP-102")["events"]
    assert event["measures"]["cumulativeMinutes"] == 24


def test_min_minutes_drops_shorter_runs(mongo_db) -> None:
    assert sensor.find_excursions(shipment_id="SHP-103", min_minutes=19)["events"] == []  # 18-minute run


def test_energy_anomaly_is_reported_as_a_number(mongo_db) -> None:
    [event] = sensor.find_energy_anomalies(shipment_id="SHP-104")["events"]
    assert event["measures"]["powerChangePct"] == pytest.approx(30.0, abs=0.5)


def test_fleet_overview(seeded) -> None:
    rows = {r["shipmentId"]: r for r in sensor.get_fleet_overview(**seeded)["shipments"]}

    assert rows["SHP-101"]["excursionCount"] == 0
    assert rows["SHP-102"]["excursionCount"] == 1
    assert rows["SHP-103"]["doorOpenMinutes"] == 15


def test_reefer_timeline_widens_buckets(seeded) -> None:
    result = sensor.get_reefer_timeline("RF-02", start=seeded["start"], end=seeded["end"])

    assert result["bucketMinutes"] == 45
    assert 0 < len(result["buckets"]) <= sensor.MAX_BUCKETS
    assert max(b["maxTemp"] for b in result["buckets"]) > 4  # the excursion shows up


def test_unknown_shipment_is_not_found(mongo_db) -> None:
    with pytest.raises(ToolError) as exc_info:
        sensor.find_excursions(shipment_id="SHP-999")
    assert exc_info.value.code == "NOT_FOUND"