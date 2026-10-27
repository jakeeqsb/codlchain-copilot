# Ontology

The ontology defines **what things are, how they connect, and how a clause becomes a rule the database can evaluate**. With it, the question "does Article 6 apply?" is answered by data, not by the LLM reading a sentence.

MVP scope has three parts:

1. **Classes** — a hierarchy for cargo and events
2. **Relations** — six named relations, stored in existing fields
3. **Rules** — a fixed format for clause triggers, actions, and deadlines

Out of scope: a separate edge collection and OR/nested conditions. In the MVP, clauses are written by hand; AI-assisted extraction is an optional last step (§ 7).

> **Note on ontology versioning.** The vocabulary itself (class names, measure names, detection rules) is fixed during the MVP, so it carries no version. MongoDB can version it with standard document patterns when needed: a version field on each ontology document (Schema Versioning / Document Versioning patterns), an `ontologyVersion` on each clause to record which vocabulary it was written against, and a history collection for retired definitions. Contract versions (`v1`, `v2`) are already tracked separately.

---

## 1. Classes

Only cargo and events need a hierarchy. Other entities (Reefer, Shipment, Shipper, ContractVersion, Clause) are flat types.

```text
cargo:seafood
├── cargo:live_seafood
│   ├── cargo:live_fish
│   └── cargo:oyster
└── cargo:frozen_seafood
    ├── cargo:frozen_tuna
    └── cargo:frozen_shrimp

event:incident
├── event:temperature_excursion
├── event:door_opening
├── event:compressor_stop
└── event:humidity_breach        (defined, but no humidity sensor in the MVP)
event:equipment_signal
└── event:power_anomaly
```

```js
{ _id: "cargo:live_fish", kind: "class", parent: "cargo:live_seafood",
  ancestors: ["cargo:live_seafood", "cargo:seafood"] }

// Run pattern: consecutive readings that match a condition form one event
{ _id: "event:temperature_excursion", kind: "class", parent: "event:incident",
  ancestors: ["event:incident"], requiresFields: ["temp"],
  detect: { pattern: "run",
            match: { $expr: { $or: [ { $gt: ["$temp", "$meta.temperaturePolicy.maxC"] },
                                     { $lt: ["$temp", "$meta.temperaturePolicy.minC"] } ] } },
            durationAs: "consecutiveMinutes",          // length of each run
            cumulativeAs: "cumulativeMinutes",         // sum of runs in the shipment period
            summary: { peakTemp: { $max: "$temp" }, lowestTemp: { $min: "$temp" },
                       direction: { $first: { $cond: [ { $gt: ["$temp", "$meta.temperaturePolicy.maxC"] },
                                                       "above", "below" ] } } },
            causeFrom: ["event:compressor_stop", "event:door_opening"] } }   // overlapping run = cause

{ _id: "event:door_opening", kind: "class", parent: "event:incident",
  ancestors: ["event:incident"], requiresFields: ["doorOpen"],
  detect: { pattern: "run", durationAs: "doorOpenMinutes", match: { doorOpen: true } } }

{ _id: "event:compressor_stop", kind: "class", parent: "event:incident",
  ancestors: ["event:incident"], requiresFields: ["powerKw"],
  detect: { pattern: "run", durationAs: "stopMinutes", match: { powerKw: { $lt: 0.1 } } } }

// Trend pattern: recent average compared with a baseline window
{ _id: "event:power_anomaly", kind: "class", parent: "event:equipment_signal",
  ancestors: ["event:equipment_signal"], requiresFields: ["powerKw"],
  detect: { pattern: "trend", field: "powerKw", recentHours: 24, baselineHours: 48,
            changeAs: "powerChangePct" } }

// Defined but not detectable in the MVP: no humidity sensor
{ _id: "event:humidity_breach", kind: "class", parent: "event:incident",
  ancestors: ["event:incident"], requiresFields: ["humidityPct"],
  detect: { pattern: "run", match: { humidityPct: { $lt: 90 } }, durationAs: "humidityMinutes",
            summary: { minHumidityPct: { $min: "$humidityPct" } } } }
```

**Design choices**
- `ancestors` is stored precomputed. Finding clauses for a cargo is one `$in` query, with no traversal. The hierarchy rarely changes, so the read path stays simple.
- The full catalog lives in `data/ontology.json` and is loaded by `seed.py`, which rejects any clause that uses a name the ontology does not define.
- Every event class carries its own detection rule (`detect`). A run rule can also declare `summary` measures (MQL accumulators per run), a `cumulativeAs` total, and `causeFrom` event types whose overlapping runs name the cause. The code has only two generic patterns: **run** (consecutive readings matching a condition, same gap rule as below) and **trend** (recent average vs. a baseline). Adding an event type means adding a document, not code. The `match` condition is MQL, so it goes straight into the pipeline.
- `requiresFields` tells the evaluator whether "no event" can be trusted. If the field is never collected, "no event" means "unknown," not "nothing happened."

## 2. Relations

No edge collection. Each relation is an existing field; the ontology records what the field means.

| Relation | From → To | Stored at | Traversal |
|---|---|---|---|
| `assignedTo` | Reefer → Shipment | `shipments.reeferId` | lookup |
| `governedBy` | Shipment → ContractVersion | `shipments.contractRef` | lookup |
| `hasClause` | ContractVersion → Clause | `clauses.contractId` + `version` | lookup |
| `appliesTo` | Clause → Cargo class | `clauses.appliesTo[]` | `$in` with ancestors |
| `triggeredBy` | Clause → Event class | `clauses.trigger.eventType` | match |
| `references` | Clause → Clause | `clauses.references[]` | `$graphLookup` |

```js
{ _id: "rel:governedBy", kind: "relation", domain: "Shipment",
  range: "ContractVersion", cardinality: "one", storedAt: "shipments.contractRef" }
```

`references` is the only multi-hop relation. Finding the clauses that depend on a triggered clause walks it in reverse:

```js
db.clauses.aggregate([
  { $match: { clauseId: "blueharbor-live-fish-v1-art6" } },
  { $graphLookup: {
      from: "clauses",
      startWith: "$clauseId",
      connectFromField: "clauseId",
      connectToField: "references",          // who references me?
      as: "dependents",
      maxDepth: 2,
      depthField: "depth"
  }}
])
// → art7 (depth 0): quality inspection depends on art6
```

## 3. Measure Vocabulary

Detection tools and clause triggers use **the same measure names**. This is the most important rule in the ontology: if detection reports `minutesOutOfRange` but a clause asks for `consecutiveMinutes`, the link breaks.

| Measure | Event class | Unit | Definition |
|---|---|---|---|
| `direction` | temperature_excursion | — | `above` (temp > maxC) or `below` (temp < minC) |
| `consecutiveMinutes` | temperature_excursion | min | Length of one excursion: last − first out-of-range reading + 1 |
| `cumulativeMinutes` | temperature_excursion | min | Sum of `consecutiveMinutes` over all excursions in the shipment period (`departedAt` to `deliveredAt`, or now) |
| `peakTemp` / `lowestTemp` | temperature_excursion | °C | Max / min temperature during the excursion |
| `cause` | temperature_excursion | — | `cause:compressor_stop`, `cause:door_opening`, or `cause:unknown`, from overlapping events |
| `doorOpenMinutes` | door_opening | min | Length of one continuous door-open run |
| `stopMinutes` | compressor_stop | min | Length of one run with `powerKw < 0.1` |
| `powerChangePct` | power_anomaly | % | Last 24 h average power vs. the preceding 48 h, as a percentage change |
| `humidityMinutes` | humidity_breach | min | Length of one run with humidity below 90% (no sensor in the MVP) |
| `minHumidityPct` | humidity_breach | % | Lowest humidity during the run |

**Continuity rule.** A run event (excursion, door opening, compressor stop) is a run of consecutive matching readings of one shipment. A gap of more than 2 minutes between readings ends the run. A reading equal to a bound is in range.

## 4. Rule Format (`clauses`)

```js
// Contract Article 6: notify within 1 h if above 4°C for 15+ consecutive minutes
{
  clauseId: "blueharbor-live-fish-v1-art6",
  contractId: "blueharbor-live-fish", version: "v1",
  chunkId: "blueharbor-live-fish-v1-article-6",
  kind: "obligation",
  appliesTo: ["cargo:live_fish"],
  trigger: {
    eventType: "event:temperature_excursion",
    allOf: [
      { measure: "direction",          op: "eq",  value: "above" },
      { measure: "consecutiveMinutes", op: "gte", value: 15 }
    ]
  },
  action:   { type: "notify", party: "shipper.qa" },
  deadline: { anchor: "eventStart", offsetMinutes: 60 },
  references: [],
  review: { status: "approved" }
}

// Article 7: quality inspection if Article 6 was triggered
{
  clauseId: "blueharbor-live-fish-v1-art7",
  contractId: "blueharbor-live-fish", version: "v1",
  chunkId: "blueharbor-live-fish-v1-article-7",
  kind: "obligation",
  appliesTo: ["cargo:live_fish"],
  trigger: { clauseTriggered: "blueharbor-live-fish-v1-art6" },
  action:  { type: "inspect", party: "shipper.qa", before: "unloading" },
  references: ["blueharbor-live-fish-v1-art6"],
  review: { status: "approved" }
}
```

| Field | Allowed values |
|---|---|
| `kind` | `obligation` (contracts → Action Required), `recommendation` (SOPs, manuals → Suggestion) |
| `trigger.eventType` | an event class |
| `trigger.allOf[].op` | `eq`, `gt`, `gte`, `lt`, `lte` |
| `trigger.clauseTriggered` | a `clauseId` (dependency) |
| `action.type` | `notify`, `inspect`, `hold`, `report`, `maintain`, `retrain` |
| `action.party` | `shipper.qa`, `shipper.logistics`, `ops.driver`, `ops.maintenance`, `ops.qa` |
| `deadline.anchor` | `eventStart`, `eventEnd` (no detection-time anchor: incidents are not stored, see decisions.md § 2) |

Conditions combine with AND only. A clause that cannot be expressed in this format stays text-only in `knowledge`; the LLM may cite it but the evaluator does not decide it.

## 5. Evaluation

`evaluate_obligations(shipment_id)` decides every applicable clause for one shipment.

1. **Resolve scope.** Read the shipment: `contractRef`, `cargoType` (already a class id such as `cargo:live_fish`), and that class's `ancestors`.
2. **Select clauses.** Approved clauses where `(contractId, version)` equals `contractRef`, plus clauses without `contractId` whose `appliesTo` intersects the cargo class and its ancestors.
3. **Collect events.** For each event class the selected clauses need, run its `detect` pattern over the shipment period.
4. **Evaluate triggers.**

| Situation | Status |
|---|---|
| An event of the trigger type satisfies every condition | `triggered` |
| Events exist, none satisfies the conditions | `not_triggered` (report the closest value, e.g. "24 min < 30 min") |
| No event of that type, and the required data was collected | `not_triggered` ("no event") |
| The required data was never collected, or a measure is missing | `unresolved` |
| Dependency: referenced clause is `triggered` / `unresolved` / `not_triggered` | same status |

5. **Compute deadlines** from the matched event: `anchor` time + `offsetMinutes`.
6. **Return** each clause with status, the matched event, the fact-versus-condition comparison, the deadline, and `chunkId` so the LLM can quote the source.

`unresolved` is never reported as "no obligation."

## 6. Expected Results on Seeded Data

| Shipment | Clause | Status | Why |
|---|---|---|---|
| SHP-102 | BlueHarbor Art. 6 notify | triggered | 24 consecutive min ≥ 15 |
| SHP-102 | BlueHarbor Art. 7 inspect | triggered | depends on Art. 6 |
| SHP-102 | BlueHarbor Art. 8 hold | not_triggered | 24 cumulative min < 30 |
| SHP-102 | SOP § 2 compressor stop | triggered | compressor stopped 30 min |
| SHP-102 | SOP § 4 incident report | triggered | excursion occurred |
| SHP-102 | Manual § 4 inspect before reassignment | triggered | compressor stopped |
| SHP-103 | OceanPeak Art. 5 notify | triggered | above −50°C |
| SHP-103 | OceanPeak Art. 6 inspection | triggered | 18 cumulative min ≥ 10 |
| SHP-103 | OceanPeak Art. 7 door report | triggered | door open 15 min > 10 |
| SHP-103 | SOP § 3 retrain, § 4 report | triggered | door open 15 min; excursion occurred |
| SHP-104 | CoastalFresh Art. 5 report | triggered | power +30% ≥ 20% |
| SHP-104 | Manual § 2 schedule maintenance | triggered | power +30% ≥ 20% |
| SHP-104 | Manual § 3 stop assigning | not_triggered | +30% < 40% |
| SHP-105 | TidalPearl Art. 5 humidity | **unresolved** | humidity is not collected |
| SHP-105 | TidalPearl Art. 6 notify | not_triggered | no excursion |
| SHP-101 | BlueHarbor Art. 6 – 8 | not_triggered | no event |

Text-only articles (rules that are not sensor-decidable, such as BlueHarbor Art. 9 "records on request") have no clause and are cited, not decided.

Exact values depend on the seed run; the statuses should not.

## 7. Optional Extensions

- **New cargo, no new documents.** Add `cargo:frozen_squid` under `cargo:frozen_seafood` and one shipment. Frozen-seafood SOP clauses apply through `ancestors`.
- **Contract versions.** Add `oceanpeak-tuna` v2 with a different threshold and compare outcomes for the same sensor facts.
- **AI-assisted clause extraction.** In production, an LLM drafts each clause from its source text using only this ontology's vocabulary, the draft is validated automatically and stored with `review.status: "pending"`, and a person approves it before it is used. The data model already supports this (`review.status`, `chunkId`), so adding it needs one script (`extract_clauses.py`), not a schema change.
