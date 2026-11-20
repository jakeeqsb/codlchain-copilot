"""Unit tests for coldchain_copilot.tools.scope: scope filters and windows.

These tests use no database. ``resolve`` with a shipment id is covered by the
sensor tool integration tests, which run against seeded shipments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coldchain_copilot.errors import ToolError
from coldchain_copilot.tools import scope

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)


# --- meta_filter -------------------------------------------------------------


def test_each_id_maps_to_its_meta_field() -> None:
    assert scope.meta_filter(fleet_id="FLEET-01") == {"meta.fleetId": "FLEET-01"}
    assert scope.meta_filter(reefer_id="RF-02") == {"meta.reeferId": "RF-02"}
    assert scope.meta_filter(shipment_id="SHP-102") == {"meta.shipmentId": "SHP-102"}


def test_several_ids_narrow_together() -> None:
    assert scope.meta_filter(fleet_id="FLEET-01", reefer_id="RF-02") == {
        "meta.fleetId": "FLEET-01",
        "meta.reeferId": "RF-02",
    }


@pytest.mark.parametrize("ids", [{}, {"fleet_id": None}, {"reefer_id": ""}])
def test_missing_scope_is_rejected_not_widened(ids: dict) -> None:
    with pytest.raises(ToolError) as exc_info:
        scope.meta_filter(**ids)
    assert exc_info.value.code == "SCOPE_REQUIRED"


# --- parse_time --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["2026-10-15T00:12:00Z", "2026-10-15T09:12:00+09:00", "2026-10-15T00:12:00"],
)
def test_any_offset_becomes_the_same_utc_instant(text: str) -> None:
    assert scope.parse_time(text) == datetime(2026, 10, 15, 0, 12, tzinfo=timezone.utc)


def test_bad_time_string_raises() -> None:
    with pytest.raises(ValueError):
        scope.parse_time("yesterday")


# --- window ------------------------------------------------------------------


def test_default_is_last_24_hours() -> None:
    assert scope.window(max_hours=72, now=NOW) == (NOW - timedelta(hours=24), NOW)


def test_hours_ends_now() -> None:
    assert scope.window(hours=6, max_hours=72, now=NOW) == (NOW - timedelta(hours=6), NOW)


def test_explicit_times_win_over_hours() -> None:
    start, end = scope.window(
        hours=6,
        start="2026-10-14T00:00:00Z",
        end="2026-10-14T12:00:00Z",
        max_hours=72,
        now=NOW,
    )
    assert (start, end) == (
        datetime(2026, 10, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
    )


def test_missing_end_is_now_and_missing_start_is_default_before_end() -> None:
    assert scope.window(start="2026-10-15T00:00:00Z", max_hours=72, now=NOW)[1] == NOW

    start, end = scope.window(end="2026-10-14T12:00:00Z", max_hours=72, now=NOW)
    assert end - start == timedelta(hours=scope.DEFAULT_HOURS)


def test_exactly_max_hours_is_allowed() -> None:
    start, end = scope.window(hours=72, max_hours=72, now=NOW)
    assert end - start == timedelta(hours=72)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hours": 73},
        {"start": "2026-10-10T00:00:00Z", "end": "2026-10-15T00:00:00Z"},
    ],
)
def test_window_over_max_is_rejected(kwargs: dict) -> None:
    with pytest.raises(ToolError) as exc_info:
        scope.window(**kwargs, max_hours=72, now=NOW)
    assert exc_info.value.code == "WINDOW_TOO_LARGE"


# --- shipment_period ---------------------------------------------------------


def test_delivered_shipment_runs_departure_to_delivery() -> None:
    departed = datetime(2026, 10, 12, tzinfo=timezone.utc)
    delivered = datetime(2026, 10, 14, tzinfo=timezone.utc)
    shipment = {"departedAt": departed, "deliveredAt": delivered}
    assert scope.shipment_period(shipment, now=NOW) == (departed, delivered)


def test_shipment_in_transit_runs_to_now_and_is_not_capped() -> None:
    departed = NOW - timedelta(hours=100)  # longer than MAX_WINDOW_HOURS on purpose
    assert scope.shipment_period({"departedAt": departed}, now=NOW) == (departed, NOW)