"""
Seed ColdChain Copilot.

    uv run seed.py            # validate the catalog, then write everything to MongoDB
    uv run seed.py --dry-run  # validate and simulate only; print scenario facts, no database

Writes five collections (knowledge is written by ingest_docs.py):
    cargo_specs  default temperature ranges by cargo class
    shipments    five shipments, policy resolved, departed 72 h before the run
    ontology     classes, detection rules, measures, relations, vocabulary   (data/ontology.json)
    clauses      contract and SOP clauses as rules                            (data/clauses.json)
    readings     Time Series, one reading per reefer per minute for 72 h, policy snapshot in meta

Demo situations, timed relative to the run:
    RF-02  compressor stops for 30 min about 5 h ago   -> temperature excursion
    RF-03  door open 15 min during unloading ~3 h ago  -> temperature excursion
    RF-04  power drifts up over the last 24 h           -> power anomaly, temperature normal
    RF-05  no humidity sensor                           -> humidity clause cannot be decided
"""
import argparse
import json
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
HISTORY_HOURS = 72
OPS = {"eq", "gt", "gte", "lt", "lte"}

# Simple thermal model, per minute
K_COOL = 0.30       # pull toward the setpoint while the compressor runs
K_LEAK = 0.02       # heat leak from outside air
K_LEAK_DOOR = 0.08  # heat leak with the door open

# Per-reefer simulation settings: setpoint inside the policy range, compressor base power
SIM = {
    "RF-01": {"setpointC": 1.0,   "basePowerKw": 2.0},
    "RF-02": {"setpointC": 1.0,   "basePowerKw": 2.1},
    "RF-03": {"setpointC": -58.0, "basePowerKw": 7.0},
    "RF-04": {"setpointC": -23.0, "basePowerKw": 4.5},
    "RF-05": {"setpointC": 2.0,   "basePowerKw": 1.9},
}


# ---------------------------------------------------------------- loading
def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def doc_chunk_ids():
    """chunkIds the docs will produce: <docId>-<version>-article-N / section-N."""
    ids = set()
    for f in sorted(DOCS.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        front = dict(re.findall(r"^(\w+):\s*(.+)$", text.split("---")[1], re.M))
        for kind, num in re.findall(r"^## (Article|Section) (\d+):", text, re.M):
            ids.add(f"{front['docId']}-{front['version']}-{kind.lower()}-{num}")
    return ids


# ---------------------------------------------------------------- validation
def validate_catalog(ontology, clauses, shipments, cargo_specs):
    """Every clause must use only names the ontology defines and point to real source text."""
    by_id = {d["_id"]: d for d in ontology}
    classes = {i for i, d in by_id.items() if d["kind"] == "class"}
    measures = {i.split(":", 1)[1]: d for i, d in by_id.items() if d["kind"] == "measure"}
    actions = {i.split(":", 1)[1] for i, d in by_id.items() if d["kind"] == "action"}
    parties = {i.split(":", 1)[1] for i, d in by_id.items() if d["kind"] == "party"}
    clause_ids = {c["clauseId"] for c in clauses}
    chunks = doc_chunk_ids()
    errors = []

    for c in clauses:
        cid, trig = c["clauseId"], c["trigger"]
        if c["chunkId"] not in chunks:
            errors.append(f"{cid}: chunkId {c['chunkId']} not found in docs/")
        for cls in c["appliesTo"]:
            if cls not in classes:
                errors.append(f"{cid}: unknown class {cls}")
        if c["action"]["type"] not in actions:
            errors.append(f"{cid}: unknown action {c['action']['type']}")
        if c["action"]["party"] not in parties:
            errors.append(f"{cid}: unknown party {c['action']['party']}")
        for ref in c.get("references", []):
            if ref not in clause_ids:
                errors.append(f"{cid}: references unknown clause {ref}")
        if "clauseTriggered" in trig:
            if trig["clauseTriggered"] not in clause_ids:
                errors.append(f"{cid}: depends on unknown clause {trig['clauseTriggered']}")
            continue
        etype = trig["eventType"]
        if "detect" not in by_id.get(etype, {}):
            errors.append(f"{cid}: event {etype} has no detection rule")
        for cond in trig["allOf"]:
            m = measures.get(cond["measure"])
            if not m:
                errors.append(f"{cid}: unknown measure {cond['measure']}")
            elif m["eventType"] != etype:
                errors.append(f"{cid}: measure {cond['measure']} belongs to {m['eventType']}, not {etype}")
            if cond["op"] not in OPS:
                errors.append(f"{cid}: unknown op {cond['op']}")

    contracts = {(c["contractId"], c["version"]) for c in clauses if c["contractId"]}
    specs = {s["cargoType"] for s in cargo_specs}
    for s in shipments:
        ref = (s["contractRef"]["contractId"], s["contractRef"]["version"])
        if ref not in contracts:
            errors.append(f"{s['shipmentId']}: contract {ref} has no clauses")
        if s["cargoType"] not in classes:
            errors.append(f"{s['shipmentId']}: cargoType {s['cargoType']} is not an ontology class")
        if s["cargoType"] not in specs:
            errors.append(f"{s['shipmentId']}: no cargo spec for {s['cargoType']}")

    if errors:
        raise SystemExit("Catalog validation failed:\n  " + "\n  ".join(errors))


def resolve_shipments(shipments, cargo_specs, now):
    """Fix each shipment's policy from its cargo default (MVP) and set the shipment period."""
    specs = {s["cargoType"]: s for s in cargo_specs}
    out = []
    for s in shipments:
        spec = specs[s["cargoType"]]
        policy = spec["temperaturePolicy"]
        if policy["minC"] > policy["maxC"]:
            raise SystemExit(f"{s['shipmentId']}: minC > maxC")
        out.append({**s,
                    "temperaturePolicy": {**policy, "sourceSpecVersion": spec["specVersion"],
                                          "policyVersion": f"{s['shipmentId']}-policy-v1"},
                    "departedAt": now - timedelta(hours=HISTORY_HOURS),
                    "deliveredAt": None})
    return out


def check_reading(reading, shipment):
    """Reject a reading whose policy snapshot does not match its shipment."""
    snap = reading["meta"]["temperaturePolicy"]
    pol = shipment["temperaturePolicy"]
    if (snap["policyVersion"], snap["minC"], snap["maxC"]) != (pol["policyVersion"], pol["minC"], pol["maxC"]):
        raise ValueError(f"policy mismatch for {shipment['shipmentId']}")


# ---------------------------------------------------------------- simulation
def ambient(t):
    hour_kst = (t.hour + 9) % 24
    return 15 + 4 * math.sin((hour_kst - 9) / 24 * 2 * math.pi)


def simulate(shipment, now, rng):
    rid = shipment["reeferId"]
    setpoint, base_power = SIM[rid]["setpointC"], SIM[rid]["basePowerKw"]
    start = shipment["departedAt"]
    n = HISTORY_HOURS * 60

    compressor_off = range(n - 300, n - 270) if rid == "RF-02" else range(0)
    door_open = range(n - 180, n - 165) if rid == "RF-03" else range(0)

    meta = {"fleetId": shipment["fleetId"], "reeferId": rid, "shipmentId": shipment["shipmentId"],
            "temperaturePolicy": {k: shipment["temperaturePolicy"][k] for k in ("minC", "maxC", "policyVersion")}}

    temp = eq_offset = None
    readings = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        amb = ambient(ts)
        cooling = i not in compressor_off
        door = i in door_open
        if temp is None:  # start at the steady state
            temp = (K_COOL * setpoint + K_LEAK * amb) / (K_COOL + K_LEAK)
            eq_offset = temp - setpoint
        leak = K_LEAK_DOOR if door else K_LEAK
        temp += (K_COOL * (setpoint - temp) if cooling else 0) + leak * (amb - temp)

        if cooling:
            power = base_power * (2.2 if door else min(2.5, 1 + 0.15 * max(0.0, temp - (setpoint + eq_offset))))
            if rid == "RF-04":  # efficiency loss: power ramps from 1.0x to 1.6x over the last 24 h
                power *= 1 + 0.6 * max(0, i - (n - 24 * 60)) / (24 * 60)
            power += rng.gauss(0, base_power * 0.03)
        else:
            power = abs(rng.gauss(0, 0.02))

        readings.append({"ts": ts, "meta": meta,
                         "temp": round(temp + rng.gauss(0, 0.1), 2),
                         "powerKw": round(max(0.0, power), 3),
                         "doorOpen": door})
    return readings


# ---------------------------------------------------------------- dry-run facts
def runs(readings, pred, gap=timedelta(minutes=2)):
    """Consecutive matching readings, split when the gap between matches exceeds 2 min."""
    out, cur = [], None
    for r in readings:
        if not pred(r):
            continue
        if cur and r["ts"] - cur[1] <= gap:
            cur[1] = r["ts"]
        else:
            cur = [r["ts"], r["ts"]]
            out.append(cur)
    return [(a, b, int((b - a).total_seconds() // 60) + 1) for a, b in out]


def facts(shipment, readings, now):
    pol = shipment["temperaturePolicy"]
    exc = runs(readings, lambda r: r["temp"] > pol["maxC"] or r["temp"] < pol["minC"])
    door = runs(readings, lambda r: r["doorOpen"])
    stop = runs(readings, lambda r: r["powerKw"] < 0.1)
    recent = [r["powerKw"] for r in readings if r["ts"] >= now - timedelta(hours=24)]
    base = [r["powerKw"] for r in readings if now - timedelta(hours=72) <= r["ts"] < now - timedelta(hours=24)]
    change = (sum(recent) / len(recent)) / (sum(base) / len(base)) * 100 - 100
    return {"excursions": [(m, max(r["temp"] for r in readings if a <= r["ts"] <= b)) for a, b, m in exc],
            "cumulativeMinutes": sum(m for _, _, m in exc),
            "doorOpenMinutes": [m for _, _, m in door],
            "stopMinutes": [m for _, _, m in stop],
            "powerChangePct": round(change, 1)}


# ---------------------------------------------------------------- write
def write(db, cargo_specs, shipments, ontology, clauses, readings_by_shipment):
    for name in ("cargo_specs", "shipments", "ontology", "clauses", "readings"):
        db.drop_collection(name)

    db.cargo_specs.insert_many([dict(s) for s in cargo_specs])
    db.shipments.insert_many([dict(s) for s in shipments])
    db.shipments.create_index("shipmentId", unique=True)
    db.shipments.create_index("reeferId")

    # detect.match holds MQL such as {"$expr": ...}; storing $-prefixed field names needs MongoDB 5.0+
    db.ontology.insert_many([dict(d) for d in ontology])
    db.ontology.create_index("kind")

    db.clauses.insert_many([dict(c) for c in clauses])
    db.clauses.create_index("clauseId", unique=True)
    db.clauses.create_index([("contractId", 1), ("version", 1)])
    db.clauses.create_index("appliesTo")

    db.create_collection("readings",
                         timeseries={"timeField": "ts", "metaField": "meta", "granularity": "minutes"})
    db.readings.create_index([("meta.fleetId", 1), ("ts", 1)])
    db.readings.create_index([("meta.reeferId", 1), ("ts", 1)])
    for readings in readings_by_shipment.values():  # one shipment at a time = same meta per batch
        for i in range(0, len(readings), 1000):
            db.readings.insert_many(readings[i:i + 1000], ordered=False)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="validate and simulate, no database")
    args = ap.parse_args()

    cargo_specs, ontology = load("cargo_specs.json"), load("ontology.json")
    clauses, raw_shipments = load("clauses.json"), load("shipments.json")
    validate_catalog(ontology, clauses, raw_shipments, cargo_specs)
    print(f"Catalog OK: {len(ontology)} ontology docs, {len(clauses)} clauses, {len(raw_shipments)} shipments")

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    shipments = resolve_shipments(raw_shipments, cargo_specs, now)
    rng = random.Random(42)
    readings_by_shipment = {}
    for s in shipments:
        readings = simulate(s, now, rng)
        for r in readings:
            check_reading(r, s)
        readings_by_shipment[s["shipmentId"]] = readings

    for s in shipments:
        f = facts(s, readings_by_shipment[s["shipmentId"]], now)
        print(f"  {s['shipmentId']} {s['reeferId']} {s['cargoType']:<20} "
              f"excursions={f['excursions']} cumulative={f['cumulativeMinutes']} "
              f"door={f['doorOpenMinutes']} stop={f['stopMinutes']} power={f['powerChangePct']:+}%")

    if args.dry_run:
        return

    from dotenv import load_dotenv
    from pymongo import MongoClient
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"])[os.getenv("DB_NAME", "coldchain_copilot")]
    write(db, cargo_specs, shipments, ontology, clauses, readings_by_shipment)
    total = sum(len(r) for r in readings_by_shipment.values())
    print(f"Wrote {total:,} readings to {db.name}.readings (Time Series)")
    try:
        stats = db.command("collStats", "readings").get("timeseries", {})
        print(f"Buckets: {stats.get('bucketCount')}  avg bucket size: {stats.get('avgBucketSize')} bytes")
    except Exception:
        pass


if __name__ == "__main__":
    main()
