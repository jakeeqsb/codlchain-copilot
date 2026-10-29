"""Seed MongoDB with the demo catalog and 72 hours of simulated sensor readings.

Usage (from the repository root):

    uv run scripts/seed.py            # validate, simulate, write to MongoDB
    uv run scripts/seed.py --dry-run  # validate and simulate only; no writes

Paths and the connection come from ``.env`` (DATA_DIR, DOCS_DIR, MONGODB_URI,
DB_NAME). ``knowledge`` is not written here; scripts/ingest_docs.py does that.

Collections written (each is dropped and recreated):
    cargo_specs  default temperature policy per cargo class
    shipments    five shipments, policy resolved, departed 72 h before the run
    ontology     classes, detect rules, measures, relations, vocabulary
    clauses      contract, SOP, and manual clauses as rules
    readings     Time Series, one reading per reefer per minute, policy in meta

Demo situations, timed relative to the run:
    RF-02  compressor stops for 30 min about 5 h ago  -> temperature excursion
    RF-03  door open 15 min about 3 h ago             -> temperature excursion
    RF-04  power drifts up over the last 24 h          -> power anomaly only
    RF-05  no humidity sensor                          -> humidity clause unresolved
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.database import Database

from coldchain_copilot import db
from coldchain_copilot.catalog import (
    Catalog,
    CatalogError,
    doc_chunk_ids,
    load_catalog,
    validate_catalog,
)
from coldchain_copilot.config import ConfigError, get_settings

Doc = dict[str, Any]

HISTORY_HOURS = 72
"""Length of simulated history. MAX_WINDOW_HOURS defaults to the same value."""

INSERT_BATCH = 1000

AMBIENT_END_HOUR_KST = 14.5
"""Local hour (14:30) assigned to the end of the simulated history.

Outside temperature follows a daily cycle. Anchoring that cycle to the run time
would make the excursion lengths change with the hour seed.py is run; anchoring
it to a fixed virtual clock keeps every run identical (RF-02: 24 min).
"""

# Thermal model, applied once per simulated minute.
K_COOL = 0.30  # pull toward the setpoint while the compressor runs
K_LEAK = 0.02  # heat leak from outside air, door closed
K_LEAK_DOOR = 0.08  # heat leak from outside air, door open

SIM: dict[str, dict[str, float]] = {
    "RF-01": {"setpointC": 1.0, "basePowerKw": 2.0},
    "RF-02": {"setpointC": 1.0, "basePowerKw": 2.1},
    "RF-03": {"setpointC": -58.0, "basePowerKw": 7.0},
    "RF-04": {"setpointC": -23.0, "basePowerKw": 4.5},
    "RF-05": {"setpointC": 2.0, "basePowerKw": 1.9},
}
"""Per-reefer setpoint (inside the policy range) and compressor base power."""


# --------------------------------------------------------------------- resolve


def resolve_shipments(catalog: Catalog, now: datetime) -> list[Doc]:
    """Fixes each shipment's temperature policy and shipment period.

    In the MVP the policy comes from the cargo spec. The policy gets a
    shipment-specific ``policyVersion`` so readings can carry a snapshot of it.

    Args:
        catalog: A validated catalog.
        now: The run time; shipments depart ``HISTORY_HOURS`` before it.

    Returns:
        New shipment documents with ``temperaturePolicy``, ``departedAt``,
        and ``deliveredAt`` (None: still in transit).

    Raises:
        ValueError: A cargo spec has ``minC`` greater than ``maxC``.
    """
    specs = {s["cargoType"]: s for s in catalog.cargo_specs}
    resolved = []
    for shipment in catalog.shipments:
        spec = specs[shipment["cargoType"]]
        policy = spec["temperaturePolicy"]
        if policy["minC"] > policy["maxC"]:
            raise ValueError(f"{spec['specVersion']}: minC > maxC")
        resolved.append(
            {
                **shipment,
                "temperaturePolicy": {
                    **policy,
                    "sourceSpecVersion": spec["specVersion"],
                    "policyVersion": f"{shipment['shipmentId']}-policy-v1",
                },
                "departedAt": now - timedelta(hours=HISTORY_HOURS),
                "deliveredAt": None,
            }
        )
    return resolved


def check_reading(reading: Doc, shipment: Doc) -> None:
    """Rejects a reading whose policy snapshot differs from its shipment.

    Detection compares ``temp`` with ``meta.temperaturePolicy``, so a stale
    snapshot would silently move the excursion threshold.

    Raises:
        ValueError: The snapshot's version, minC, or maxC does not match.
    """
    snap = reading["meta"]["temperaturePolicy"]
    policy = shipment["temperaturePolicy"]
    keys = ("policyVersion", "minC", "maxC")
    if tuple(snap[k] for k in keys) != tuple(policy[k] for k in keys):
        raise ValueError(f"policy snapshot mismatch for {shipment['shipmentId']}")


# ------------------------------------------------------------------ simulation


def ambient_c(minutes_before_end: int) -> float:
    """Returns outside air temperature on a fixed virtual clock.

    Args:
        minutes_before_end: Minutes between this reading and the end of the
            simulated history.

    Returns:
        15 °C ± 4 °C on a daily cycle peaking at 15:00 local time, where the
        end of the history is ``AMBIENT_END_HOUR_KST``.
    """
    hour = (AMBIENT_END_HOUR_KST - minutes_before_end / 60) % 24
    return 15 + 4 * math.sin((hour - 9) / 24 * 2 * math.pi)


def simulate(shipment: Doc, rng: random.Random) -> list[Doc]:
    """Generates one reading per minute for the shipment's reefer.

    Args:
        shipment: A resolved shipment (see :func:`resolve_shipments`).
        rng: Random source; a fixed seed makes runs repeatable.

    Returns:
        Readings in time order: ``ts``, ``meta``, ``temp``, ``powerKw``,
        ``doorOpen``. No ``humidityPct``: none of the reefers has the sensor.
    """
    reefer_id = shipment["reeferId"]
    setpoint = SIM[reefer_id]["setpointC"]
    base_power = SIM[reefer_id]["basePowerKw"]
    n = HISTORY_HOURS * 60

    compressor_off = range(n - 300, n - 270) if reefer_id == "RF-02" else range(0)
    door_open = range(n - 180, n - 165) if reefer_id == "RF-03" else range(0)

    # One meta object per shipment: every reading shares it, so all of them land
    # in the same Time Series buckets.
    meta = {
        "fleetId": shipment["fleetId"],
        "reeferId": reefer_id,
        "shipmentId": shipment["shipmentId"],
        "temperaturePolicy": {
            k: shipment["temperaturePolicy"][k] for k in ("minC", "maxC", "policyVersion")
        },
    }

    readings: list[Doc] = []
    temp: float | None = None
    offset = 0.0  # steady-state gap between setpoint and actual temperature
    for i in range(n):
        ts = shipment["departedAt"] + timedelta(minutes=i)
        outside = ambient_c(n - i)
        cooling = i not in compressor_off
        door = i in door_open
        if temp is None:
            temp = (K_COOL * setpoint + K_LEAK * outside) / (K_COOL + K_LEAK)
            offset = temp - setpoint
        leak = K_LEAK_DOOR if door else K_LEAK
        temp += (K_COOL * (setpoint - temp) if cooling else 0.0) + leak * (outside - temp)

        if cooling:
            load = 2.2 if door else min(2.5, 1 + 0.15 * max(0.0, temp - (setpoint + offset)))
            power = base_power * load
            if reefer_id == "RF-04":
                # Efficiency loss: power ramps from 1.0x to 1.6x over the last 24 h.
                power *= 1 + 0.6 * max(0, i - (n - 24 * 60)) / (24 * 60)
            power += rng.gauss(0, base_power * 0.03)
        else:
            power = abs(rng.gauss(0, 0.02))

        readings.append(
            {
                "ts": ts,
                "meta": meta,
                "temp": round(temp + rng.gauss(0, 0.1), 2),
                "powerKw": round(max(0.0, power), 3),
                "doorOpen": door,
            }
        )
    return readings


# ---------------------------------------------------------------- dry-run facts


def runs(
    readings: list[Doc], match: Callable[[Doc], bool], gap: timedelta = timedelta(minutes=2)
) -> list[int]:
    """Returns the length in minutes of each run of matching readings.

    A run ends when the next match is more than ``gap`` later. This mirrors
    the ``run`` detect pattern so dry-run output can be compared with it.
    """
    spans: list[list[datetime]] = []
    for r in readings:
        if not match(r):
            continue
        if spans and r["ts"] - spans[-1][1] <= gap:
            spans[-1][1] = r["ts"]
        else:
            spans.append([r["ts"], r["ts"]])
    return [int((end - start).total_seconds() // 60) + 1 for start, end in spans]


def scenario_facts(shipment: Doc, readings: list[Doc], now: datetime) -> dict[str, Any]:
    """Summarizes what detection should later find for one shipment."""
    policy = shipment["temperaturePolicy"]
    excursions = runs(readings, lambda r: not policy["minC"] <= r["temp"] <= policy["maxC"])
    recent_from = now - timedelta(hours=24)
    recent = [r["powerKw"] for r in readings if r["ts"] >= recent_from]
    baseline = [r["powerKw"] for r in readings if r["ts"] < recent_from]
    change = (sum(recent) / len(recent)) / (sum(baseline) / len(baseline)) * 100 - 100
    return {
        "excursions": excursions,
        "cumulative": sum(excursions),
        "door": runs(readings, lambda r: r["doorOpen"]),
        "stop": runs(readings, lambda r: r["powerKw"] < 0.1),
        "power": round(change, 1),
    }


# ----------------------------------------------------------------------- write


def write(
    database: Database, catalog: Catalog, shipments: list[Doc], readings: dict[str, list[Doc]]
) -> None:
    """Drops and recreates the seeded collections and their indexes.

    ``ontology`` stores MQL in ``detect.match`` (for example ``$expr``), so the
    server must accept ``$``-prefixed field names: MongoDB 5.0 or later.
    """
    for name in (db.CARGO_SPECS, db.SHIPMENTS, db.ONTOLOGY, db.CLAUSES, db.READINGS):
        database.drop_collection(name)

    database[db.CARGO_SPECS].insert_many([dict(s) for s in catalog.cargo_specs])

    database[db.SHIPMENTS].insert_many([dict(s) for s in shipments])
    database[db.SHIPMENTS].create_index("shipmentId", unique=True)
    database[db.SHIPMENTS].create_index("reeferId")

    database[db.ONTOLOGY].insert_many([dict(d) for d in catalog.ontology])
    database[db.ONTOLOGY].create_index("kind")

    database[db.CLAUSES].insert_many([dict(c) for c in catalog.clauses])
    database[db.CLAUSES].create_index("clauseId", unique=True)
    database[db.CLAUSES].create_index([("contractId", 1), ("version", 1)])
    database[db.CLAUSES].create_index("appliesTo")

    database.create_collection(
        db.READINGS,
        timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"},
    )
    # The automatic {meta, ts} index only serves equality on the whole meta
    # object; tools filter on meta subfields, so they get their own indexes.
    database[db.READINGS].create_index([("meta.fleetId", 1), ("ts", 1)])
    database[db.READINGS].create_index([("meta.reeferId", 1), ("ts", 1)])
    for batch_source in readings.values():  # one shipment at a time: same meta per batch
        for i in range(0, len(batch_source), INSERT_BATCH):
            database[db.READINGS].insert_many(batch_source[i : i + INSERT_BATCH], ordered=False)


def print_bucket_stats(database: Database) -> None:
    """Prints Time Series bucket statistics, if the server reports them."""
    try:
        stats = database.command("collStats", db.READINGS).get("timeseries", {})
    except Exception:  # collStats output varies by version; stats are informational
        return
    print(
        f"Buckets: {stats.get('bucketCount')}  "
        f"avg bucket size: {stats.get('avgBucketSize')} bytes"
    )


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    """Runs the seed. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="validate and simulate only")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        catalog = load_catalog(settings.data_dir)
        validate_catalog(catalog, doc_chunk_ids(settings.docs_dir))
    except (ConfigError, CatalogError, FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"Catalog OK: {len(catalog.ontology)} ontology docs, "
        f"{len(catalog.clauses)} clauses, {len(catalog.shipments)} shipments"
    )

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    shipments = resolve_shipments(catalog, now)
    rng = random.Random(42)
    readings: dict[str, list[Doc]] = {}
    for shipment in shipments:
        series = simulate(shipment, rng)
        for reading in series:
            check_reading(reading, shipment)
        readings[shipment["shipmentId"]] = series

    for shipment in shipments:
        f = scenario_facts(shipment, readings[shipment["shipmentId"]], now)
        print(
            f"  {shipment['shipmentId']} {shipment['reeferId']} {shipment['cargoType']:<20} "
            f"excursions={f['excursions']} cumulative={f['cumulative']} "
            f"door={f['door']} stop={f['stop']} power={f['power']:+}%"
        )

    if args.dry_run:
        return 0

    database = db.get_db()
    write(database, catalog, shipments, readings)
    total = sum(len(series) for series in readings.values())
    print(f"Wrote {total:,} readings to {database.name}.{db.READINGS} (Time Series)")
    print_bucket_stats(database)
    return 0


if __name__ == "__main__":
    sys.exit(main())
