"""Unit tests for coldchain_copilot.tools.sensor: scope, windows, pipeline shapes.

These tests use no database. The tools themselves run against seeded data in
tests/integration/test_sensor_db.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from coldchain_copilot.errors import ToolError
from coldchain_copilot.tools import sensor

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)


# --- meta_filter -------------------------------------------------------------


def test_each_id_maps_to_its_meta_field() -> None:
    assert sensor.meta_filter(fleet_id="FLEET-01") == {"meta.fleetId": "FLEET-01"}
    assert sensor.meta_filter(reefer_id="RF-02") == {"meta.reeferId": "RF-02"}
    assert sensor.meta_filter(shipment_id="SHP-102") == {"meta.shipmentId": "SHP-102"}


def test_several_ids_narrow_together() -> None:
    assert sensor.meta_filter(fleet_id="FLEET-01", reefer_id="RF-02") == {
        "meta.fleetId": "FLEET-01",
        "meta.reeferId": "RF-02",
    }


@pytest.mark.parametrize("ids", [{}, {"fleet_id": None}, {"reefer_id": ""}])
def test_missing_scope_is_rejected_not_widened(ids: dict) -> None:
    with pytest.raises(ToolError) as exc_info:
        sensor.meta_filter(**ids)
    assert exc_info.value.code == "SCOPE_REQUIRED"


# --- parse_time --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["2026-10-15T00:12:00Z", "2026-10-15T09:12:00+09:00", "2026-10-15T00:12:00"],
)
def test_any_offset_becomes_the_same_utc_instant(text: str) -> None:
    assert sensor.parse_time(text) == datetime(2026, 10, 15, 0, 12, tzinfo=timezone.utc)


def test_bad_time_string_raises() -> None:
    with pytest.raises(ValueError):
        sensor.parse_time("yesterday")


# --- window ------------------------------------------------------------------


def test_default_is_last_24_hours() -> None:
    assert sensor.window(max_hours=72, now=NOW) == (NOW - timedelta(hours=24), NOW)


def test_hours_ends_now() -> None:
    assert sensor.window(hours=6, max_hours=72, now=NOW) == (NOW - timedelta(hours=6), NOW)


def test_explicit_times_win_over_hours() -> None:
    start, end = sensor.window(
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
    assert sensor.window(start="2026-10-15T00:00:00Z", max_hours=72, now=NOW)[1] == NOW

    start, end = sensor.window(end="2026-10-14T12:00:00Z", max_hours=72, now=NOW)
    assert end - start == timedelta(hours=sensor.DEFAULT_HOURS)


def test_exactly_max_hours_is_allowed() -> None:
    start, end = sensor.window(hours=72, max_hours=72, now=NOW)
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
        sensor.window(**kwargs, max_hours=72, now=NOW)
    assert exc_info.value.code == "WINDOW_TOO_LARGE"


# --- shipment_period ---------------------------------------------------------


def test_delivered_shipment_runs_departure_to_delivery() -> None:
    departed = datetime(2026, 10, 12, tzinfo=timezone.utc)
    delivered = datetime(2026, 10, 14, tzinfo=timezone.utc)
    shipment = {"departedAt": departed, "deliveredAt": delivered}
    assert sensor.shipment_period(shipment, now=NOW) == (departed, delivered)


def test_shipment_in_transit_runs_to_now_and_is_not_capped() -> None:
    departed = NOW - timedelta(hours=100)  # longer than MAX_WINDOW_HOURS on purpose
    assert sensor.shipment_period({"departedAt": departed}, now=NOW) == (departed, NOW)


# --- bucket_size -------------------------------------------------------------


def test_requested_bucket_kept_when_it_fits() -> None:
    assert sensor.bucket_size(NOW - timedelta(hours=6), NOW, 5) == 5  # 72 buckets


def test_bucket_widens_to_fit_max_buckets() -> None:
    bucket = sensor.bucket_size(NOW - timedelta(hours=72), NOW, 5)
    assert bucket == 45  # 4320 minutes / 96
    assert 72 * 60 / bucket <= sensor.MAX_BUCKETS


# --- pipelines ---------------------------------------------------------------


def test_pipelines_start_with_scope_and_window() -> None:
    meta = {"meta.fleetId": "FLEET-01"}
    first = {"$match": {"meta.fleetId": "FLEET-01", "ts": {"$gte": NOW - timedelta(hours=1), "$lt": NOW}}}

    assert sensor.overview_pipeline(meta, NOW - timedelta(hours=1), NOW)[0] == first
    assert sensor.timeline_pipeline(meta, NOW - timedelta(hours=1), NOW, 5)[0] == first


def test_overview_sorts_before_group_so_last_is_latest() -> None:
    stages = [next(iter(s)) for s in sensor.overview_pipeline({}, NOW, NOW)]
    assert stages.index("$sort") < stages.index("$group")


def test_timeline_buckets_count_from_window_start() -> None:
    start = NOW - timedelta(hours=72)
    bucket_id = sensor.timeline_pipeline({}, start, NOW, 45)[1]["$group"]["_id"]

    assert bucket_id["$dateAdd"]["startDate"] == start
    index = bucket_id["$dateAdd"]["amount"]["$multiply"][0]["$floor"]["$divide"]
    assert index == [{"$subtract": ["$ts", start]}, 45 * 60_000]