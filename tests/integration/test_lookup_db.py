"""get_shipment against seeded data (needs MongoDB with scripts/seed.py loaded).

Every seeded shipment is in transit (``deliveredAt`` is None) and reefer
RF-0N carries shipment SHP-10N.
"""

from __future__ import annotations

import pytest

from coldchain_copilot.errors import ToolError
from coldchain_copilot.tools.lookup import get_shipment

pytestmark = pytest.mark.integration


def test_by_shipment_id(mongo_db) -> None:
    shipment = get_shipment(shipment_id="SHP-102")

    assert shipment["shipmentId"] == "SHP-102"
    assert shipment["reeferId"] == "RF-02"
    assert "_id" not in shipment
    assert {"minC", "maxC", "policyVersion"} <= set(shipment["temperaturePolicy"])


def test_by_reefer_returns_its_active_shipment(mongo_db) -> None:
    assert get_shipment(reefer_id="RF-03")["shipmentId"] == "SHP-103"


def test_shipment_id_wins_over_reefer_id(mongo_db) -> None:
    assert get_shipment(shipment_id="SHP-101", reefer_id="RF-05")["shipmentId"] == "SHP-101"


@pytest.mark.parametrize("kwargs", [{"shipment_id": "SHP-999"}, {"reefer_id": "RF-99"}])
def test_unknown_id_is_not_found(mongo_db, kwargs: dict) -> None:
    with pytest.raises(ToolError) as exc_info:
        get_shipment(**kwargs)
    assert exc_info.value.code == "NOT_FOUND"


def test_no_id_is_rejected(mongo_db) -> None:
    with pytest.raises(ToolError) as exc_info:
        get_shipment()
    assert exc_info.value.code == "SCOPE_REQUIRED"