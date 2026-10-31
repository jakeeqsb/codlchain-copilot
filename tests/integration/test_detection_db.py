"""Detection against seeded data (needs MongoDB with scripts/seed.py loaded).

Expected values come from the seed scenarios and the dry-run output of
scripts/seed.py. The window for each shipment is its shipment period:
departedAt to now.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coldchain_copilot import db
from coldchain_copilot.detection import detect, field_collected

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def period(mongo_db):
    """Returns a function giving (scope, start, end) for a shipment id."""

    def for_shipment(shipment_id: str):
        shipment = db.collection(db.SHIPMENTS).find_one({"shipmentId": shipment_id})
        return {"meta.shipmentId": shipment_id}, shipment["departedAt"], datetime.now(timezone.utc)

    return for_shipment


def test_rf02_excursion_caused_by_compressor_stop(period) -> None:
    [excursion] = detect("event:temperature_excursion", *period("SHP-102"))

    assert excursion["reeferId"] == "RF-02"
    assert excursion["measures"]["consecutiveMinutes"] == 24
    assert excursion["measures"]["cumulativeMinutes"] == 24
    assert excursion["measures"]["direction"] == "above"
    assert excursion["measures"]["peakTemp"] > 4
    assert excursion["measures"]["cause"] == "cause:compressor_stop"


def test_rf02_compressor_stop(period) -> None:
    [stop] = detect("event:compressor_stop", *period("SHP-102"))
    assert stop["measures"]["stopMinutes"] == 30


def test_rf03_excursion_caused_by_door(period) -> None:
    [excursion] = detect("event:temperature_excursion", *period("SHP-103"))
    [door] = detect("event:door_opening", *period("SHP-103"))

    assert excursion["measures"]["consecutiveMinutes"] == 18
    assert excursion["measures"]["cause"] == "cause:door_opening"
    assert door["measures"]["doorOpenMinutes"] == 15


def test_rf04_power_anomaly_without_excursion(period) -> None:
    [trend] = detect("event:power_anomaly", *period("SHP-104"))

    assert trend["measures"]["powerChangePct"] == pytest.approx(30.0, abs=0.5)
    assert detect("event:temperature_excursion", *period("SHP-104")) == []


def test_rf01_has_no_incidents(period) -> None:
    for event_type in ("event:temperature_excursion", "event:door_opening", "event:compressor_stop"):
        assert detect(event_type, *period("SHP-101")) == []


def test_rf05_humidity_never_collected(period) -> None:
    assert detect("event:humidity_breach", *period("SHP-105")) == []
    assert not field_collected("humidityPct", *period("SHP-105"))
    assert field_collected("temp", *period("SHP-105"))