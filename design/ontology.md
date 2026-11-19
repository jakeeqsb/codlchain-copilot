# Ontology

The ontology defines **what things are, how they connect, and what can be done to them**. With it, the question "does Article 6 apply?" is answered by data and named functions, not by the LLM reading a sentence.

It has three parts, plus rules written in its vocabulary, plus one registry in code:

| Part | Holds | Example |
|---|---|---|
| **What** | Object types and their properties; the cargo class hierarchy | `Shipment`, `Reading`; `cargo:live_fish` → `cargo:seafood` |
| **Links** | How objects connect; the traversal function for multi-hop links | `rel:references` → `follow_references` |
| **Actions** | Event types (with their detector function) and action types (with their handler function) | `event:temperature_excursion` → `detect_temperature_excursion`; `action:notify` → `notify` |
| **Rules** (`clauses`) | When an event meets conditions, which action to take, by when | "excursion ≥ 15 consecutive min → notify QA within 1 h" |
| **Registry** (`registry.py`) | Function name → Python function | `"detect_temperature_excursion"` → `detection.detect_temperature_excursion` |

**The ontology holds no logic.** It holds names and connections. Each piece of behaviour is a function named in the ontology and registered in `registry.py`; MongoDB pipelines live inside those functions. Rule *conditions* stay data (thresholds differ per contract); one generic function, `rules.py`, compares them.

Out of scope for the MVP: authoring tools. The ontology and clauses are hand-written JSON under `data/`, loaded by `scripts/seed.py`. AI-assisted clause extraction is an optional extension (§ 7).

> **Note on ontology versioning.** The vocabulary (type names, measure names, function names) is fixed during the MVP, so it carries no version. MongoDB can version it with standard document patterns when needed: a version field on each ontology document (Schema Versioning / Document Versioning patterns), an `ontologyVersion` on each clause to record which vocabulary it was written against, and a history collection for retired definitions. Contract versions (`v1`, `v2`) are already tracked separately.

---

## 1. What: Object Types and Classes

Three object types name the things the engine reads, their properties, and the collection that stores them.

```js
{ _id: "Shipment", kind: "objectType", collection: "shipments", key: "shipmentId",
  properties: { cargoType: "CargoClass", contractRef: "Contract", temperaturePolicy: "range",
                departedAt: "time", deliveredAt: "time", ... } }

{ _id: "Reading", kind: "objectType", collection: "readings", timeSeries: true,
  properties: { ts: "time", "meta.shipmentId": "string", "meta.temperaturePolicy": "range",
                temp: "number", powerKw: "number", doorOpen: "bool", humidityPct: "number" } }

{ _id: "Clause", kind: "objectType", collection: "clauses", key: "clauseId", properties: { ... } }
```

Only cargo needs a hierarchy:

```text
cargo:seafood
├── cargo:live_seafood
│   ├── cargo:live_fish
│   └── cargo:oyster
└── cargo:frozen_seafood
    ├── cargo:frozen_tuna
    └── cargo:frozen_shrimp
```

```js
{ _id: "cargo:live_fish", kind: "class", parent: "cargo:live_seafood",
  ancestors: ["cargo:live_seafood", "cargo:seafood"] }
```

**Design choices**
- `ancestors` is stored precomputed (MongoDB's *Array of Ancestors* tree pattern). Finding clauses for a cargo is one `$in` query, with no traversal. The hierarchy rarely changes, so the read path stays simple.
- `shipments.cargoType` stores the class id itself (`cargo:live_fish`), so no mapping is needed.

## 2. Links

No edge collection. Each link is an existing field; the ontology records what it connects.

| Link | From → To | Stored in | Traversal |
|---|---|---|---|
| `assignedTo` | Reefer → Shipment | `shipments.reeferId` | lookup |
| `governedBy` | Shipment → Contract | `shipments.contractRef` | lookup |
| `hasClause` | Contract → Clause | `clauses.contractId` + `version` | lookup |
| `appliesTo` | Clause → Cargo class | `clauses.appliesTo[]` | `$in` with ancestors |
| `triggeredBy` | Clause → Event type | `clauses.trigger.eventType` | match |
| `references` | Clause → Clause | `clauses.references[]` | **`follow_references`** |

```js
{ _id: "rel:governedBy", kind: "linkType", from: "Shipment", to: "Contract" }
{ _id: "rel:references", kind: "linkType", from: "Clause",   to: "Clause", traverse: "follow_references" }
```

`references` is the only multi-hop link, so it is the only one with a `traverse` function. `follow_references` (`ontology.dependent_clauses`) walks it in reverse with `$graphLookup`:

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

**Graph database swap point.** If deeper traversal is ever needed, only this function changes: register a graph-database implementation under `follow_references` (or behind a future `graph_engine` chosen by configuration). Definitions, rules, data, and every other function stay in MongoDB. See [decisions.md § 4](./decisions.md).

## 3. Actions: Event Types and Action Types

### Event types

An event type says which object it is observed on, which function detects it, which fields must be collected for "no event" to be trusted, and which measures the detector returns.

```js
{ _id: "event:temperature_excursion", kind: "eventType", on: "Reading",
  detector: "detect_temperature_excursion",
  requiresFields: ["temp"],
  measures: ["consecutiveMinutes", "cumulativeMinutes", "peakTemp", "lowestTemp", "direction", "cause"] }
```

| Event type | Detector | Measures |
|---|---|---|
| `event:temperature_excursion` | `detect_temperature_excursion` | `consecutiveMinutes`, `cumulativeMinutes`, `peakTemp`, `lowestTemp`, `direction`, `cause` |
| `event:door_opening` | `detect_door_opening` | `doorOpenMinutes` |
| `event:compressor_stop` | `detect_compressor_stop` | `stopMinutes` |
| `event:humidity_breach` | `detect_humidity_breach` | `humidityMinutes`, `minHumidityPct` (no sensor in the MVP) |
| `event:power_anomaly` | `detect_power_anomaly` | `powerChangePct` |

Detectors live in `detection.py` and share one signature, `(scope, start, end) -> events`. Each one shows its MongoDB condition and calls one of two shared pipelines:
- **`run_pipeline`**: consecutive matching readings form one event (`$shift` for gaps, a running `$sum` to number runs, `$group` per run).
- **`trend_pipeline`**: average of a field over the last hours vs. the hours before, anchored on the latest reading.

**Measure definitions**

| Measure | Event type | Unit | Definition |
|---|---|---|---|
| `direction` | temperature_excursion | — | `above` (temp > maxC) or `below` (temp < minC), from the first reading |
| `consecutiveMinutes` | temperature_excursion | min | Length of one excursion: last − first out-of-range reading + 1 |
| `cumulativeMinutes` | temperature_excursion | min | Sum of `consecutiveMinutes` over the shipment period (`departedAt` to `deliveredAt`, or now) |
| `peakTemp` / `lowestTemp` | temperature_excursion | °C | Max / min temperature during the excursion |
| `cause` | temperature_excursion | — | `cause:compressor_stop`, `cause:door_opening`, or `cause:unknown`, from overlapping events |
| `doorOpenMinutes` | door_opening | min | Length of one continuous door-open run |
| `stopMinutes` | compressor_stop | min | Length of one run with `powerKw < 0.1` |
| `powerChangePct` | power_anomaly | % | Last 24 h average power vs. the preceding 48 h, as a percentage change |
| `humidityMinutes` | humidity_breach | min | Length of one run with humidity below 90% |
| `minHumidityPct` | humidity_breach | % | Lowest humidity during the run |

Detectors and clause conditions use **the same measure names**. This is the most important rule in the ontology: if a detector returns `minutesOutOfRange` but a clause asks for `consecutiveMinutes`, the link breaks. `catalog.py` rejects a clause whose measure is not listed on its event type.

**Continuity rule.** A run event is a run of consecutive matching readings of one shipment. A gap of more than 2 minutes between readings ends the run. A reading equal to a bound is in range.

**`requiresFields`** tells the engine whether "no event" can be trusted. If the field is never collected, "no event" means "unknown," not "nothing happened."

### Action types

```js
{ _id: "action:notify", kind: "actionType", handler: "notify" }
```

Six action types: `notify`, `inspect`, `hold`, `report`, `maintain`, `retrain`. In the MVP every handler produces a briefing item (who must do what by when, with the source chunk); nothing is sent. A real deployment would register, for example, an email sender under `notify`.

Parties (`party:shipper.qa`, `party:ops.maintenance`, …) are a plain vocabulary.

## 4. Rule Format (`clauses`)

A rule is a contract or SOP article rewritten in the ontology's vocabulary: an event type and conditions on its measures, an action type and party, and a deadline. Rules are data because thresholds differ per contract; `rules.py` evaluates all of them the same way.

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
| `trigger.eventType` | an event type |
| `trigger.allOf[].measure` | a measure listed on that event type |
| `trigger.allOf[].op` | `eq`, `gt`, `gte`, `lt`, `lte` |
| `trigger.clauseTriggered` | a `clauseId` (dependency); it must also be listed in `references` |
| `action.type` | an action type: `notify`, `inspect`, `hold`, `report`, `maintain`, `retrain` |
| `action.party` | a party: `shipper.qa`, `shipper.logistics`, `ops.driver`, `ops.maintenance`, `ops.qa` |
| `deadline.anchor` | `eventStart`, `eventEnd` (no detection-time anchor: incidents are not stored, see decisions.md § 2) |

Conditions combine with AND only. An article that cannot be expressed in this format stays text-only in `knowledge`; the LLM may cite it but the engine does not decide it.

**Where rules come from.** In the MVP, by hand: read the article, express it with names from the ontology, run the catalog check, mark it approved. `catalog.py` rejects any rule that uses a name the ontology does not define, and any ontology entry whose function name is not registered.

## 5. Execution: `engine.py`

`engine.evaluate_shipment(shipment_id)` decides every applicable clause for one shipment in six steps.

| Step | What | Uses |
|---|---|---|
| 1. Object | Read the shipment; period = `departedAt` → `deliveredAt` (or now) | object type `Shipment` |
| 2. Clauses | Approved clauses of its contract version, plus contract-free clauses whose `appliesTo` meets the cargo class or its ancestors | classes, links, `ontology.applicable_clauses` |
| 3. Detect | For each event type the clauses wait on, run its `detector`; check `requiresFields` were collected | event types → `registry.DETECTORS` |
| 4. Compare | Compare each clause's conditions with the measures | rules + `rules.py` |
| 5. Follow | For each triggered clause, list clauses that reference it | link `references` → `registry.TRAVERSERS` |
| 6. Act | For each triggered clause, run its action type's handler | action types → `registry.ACTIONS` |

Step 4 statuses:

| Situation | Status |
|---|---|
| An event of the trigger type satisfies every condition | `triggered` (earliest such event; deadline = anchor + `offsetMinutes`) |
| Events exist, none satisfies the conditions | `not_triggered` (report the closest value, e.g. "24 min < 30 min") |
| No event of that type, and the required data was collected | `not_triggered` ("no event") |
| The required data was never collected, or a measure is missing | `unresolved` |
| Dependency: referenced clause is `triggered` / `unresolved` / `not_triggered` | same status |

`unresolved` is never reported as "no obligation."

The result holds each clause with its status, matched event, fact-versus-condition comparison (`basis`), deadline, `chunkId` for quoting the source, and `follows` (from step 5); plus one briefing item per triggered clause (from step 6). Nothing is stored.

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

All other selected clauses are `not_triggered`. `tests/integration/test_scenarios.py` checks the full table.

Text-only articles (rules that are not sensor-decidable, such as BlueHarbor Art. 9 "records on request") have no clause and are cited, not decided.

The seed uses a fixed virtual clock for outside temperature, so the values above (24 min, 18 min, +30%) are the same whenever it runs. Timestamps and deadlines move with the run time.

## 7. Optional Extensions

- **New cargo, no new code.** Add `cargo:frozen_squid` under `cargo:frozen_seafood` and one shipment. Frozen-seafood SOP clauses apply through `ancestors`.
- **New event type.** Add an `eventType` document and one detector function (a few lines on top of `run_pipeline` or `trend_pipeline`), and register it.
- **Contract versions.** Add `oceanpeak-tuna` v2 with a different threshold and compare outcomes for the same sensor facts.
- **Graph engine.** Put traversal behind a `graph_engine` interface and choose `$graphLookup` or a graph database by configuration. Only step 5 changes.
- **AI-assisted clause extraction.** An LLM drafts each clause from its source text using only this ontology's vocabulary, the draft is validated automatically and stored with `review.status: "pending"`, and a person approves it before it is used. The data model already supports this (`review.status`, `chunkId`), so adding it needs one script (`extract_clauses.py`), not a schema change.