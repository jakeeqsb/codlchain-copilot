# Interfaces

The LLM interacts with data only through these tools. Every tool is read-only.

Tools are thin. They resolve scope and window, call the detector or engine that
already exists, and shape the result for the LLM. They never re-implement
detection, and they never decide whether something needs action: that is the
job of the clauses, through `evaluate_obligations`.

## Tools by Level

| Level | Tool | Purpose | Backed by |
|---|---|---|---|
| 1 · Lookup | `get_shipment` | Shipment, reefer, route, policy, contract | `shipments` find |
| 2 · Sensor | `get_fleet_overview` | Per-shipment temperature, power, door summary | `readings` aggregation + `detect_temperature_excursion` |
| 2 · Sensor | `find_excursions` | Out-of-range runs as events, with measures | `detect_temperature_excursion` |
| 2 · Sensor | `find_energy_anomalies` | Power change vs. baseline, as a number | `detect_power_anomaly` |
| 2 · Sensor | `get_reefer_timeline` | Time-bucketed temperature, power, door | `readings` aggregation (`$dateTrunc`) |
| 3 · Documents | `search_knowledge` | Relevant chunks from contracts, SOPs, manuals | `$vectorSearch` (stage 4) |
| 3 · Documents | `get_clause` | Exact clause text by `clauseId` or `chunkId` | `knowledge` find (stage 4) |
| 5 · Ontology | `evaluate_obligations` | Status of every applicable clause for a shipment | `engine.evaluate_shipment` |
| Ad hoc | `run_aggregation` | Numeric questions no fixed tool covers | Text-to-MQL, guarded |

Level 4 (briefing) has no tool of its own. The agent combines levels 2, 3, and 5.

Code layout:

| Path | Holds |
|---|---|
| `src/coldchain_copilot/tools/lookup.py` | `get_shipment` |
| `src/coldchain_copilot/tools/sensor.py` | The four sensor tools and the scope and window rules they share |
| `src/coldchain_copilot/tools/adhoc.py` | `run_aggregation` and its guard |
| `src/coldchain_copilot/tools/documents.py` | `search_knowledge`, `get_clause` (stage 4) |
| `src/coldchain_copilot/tools/gateway.py` | LLM tool schemas, name → function dispatch (`evaluate_obligations` maps straight to `engine.evaluate_shipment`), error and date serialization |

---

## Common Rules

**Scope is required.** Sensor tools need at least one of `fleet_id`, `reefer_id`, or `shipment_id`. A call without scope is rejected, not widened. Scope maps directly to the reading's meta: `meta.fleetId`, `meta.reeferId`, `meta.shipmentId`.

**One window rule for every sensor tool.** A call takes either `hours` (1–72, default 24, ending now) or explicit `start` and `end` (ISO 8601 with offset) at most 72 hours apart. 72 matches the seeded history and is the `MAX_WINDOW_HOURS` setting, not a fixed rule. A `shipment_id` scope uses the shipment period instead (`departedAt` to `deliveredAt`, or now), so cumulative measures cover the whole shipment. `evaluate_obligations` always uses the shipment period.

**Times.** Storage is always UTC (BSON Date). Inputs accept any offset. Outputs are ISO 8601 strings in UTC. Every `start`, `end`, and `deadline`, the times a briefing quotes, also carries a display string in KST:

```js
start: "2026-10-15T00:12:00Z", startLocal: "2026-10-15 09:12 KST"
```

Conversion happens once, in the serializer in `tools/gateway.py`, never in individual tools and never by the LLM. The display time zone is the constant `Asia/Seoul`.

**Errors.** A tool never throws to the LLM. Tools raise `ToolError`; `tools/gateway.py` catches it and returns:

```js
{ error: { code: "SCOPE_REQUIRED", message: "Provide fleet_id, reefer_id, or shipment_id." } }
```

| Code | When |
|---|---|
| `SCOPE_REQUIRED` | No scope on a sensor tool |
| `WINDOW_TOO_LARGE` | Window over 72 hours |
| `NOT_FOUND` | Unknown shipment, reefer, clause, or tool name |
| `EVIDENCE_UNAVAILABLE` | Search index not ready or no chunks for the contract version |
| `QUERY_REJECTED` | `run_aggregation` used a forbidden stage or collection |
| `QUERY_FAILED` | Database error, or a call the function could not run (bad time string, unknown argument); message included so the LLM can retry |

---

## Level 1 · Lookup

### `get_shipment`

| Param | Type | Notes |
|---|---|---|
| `shipment_id` | string | one of the two is required |
| `reefer_id` | string | returns the reefer's active shipment |

Returns the `shipments` document as stored. The active shipment of a reefer is the one with no `deliveredAt`; if several match, the one with the latest `departedAt`. No match is `NOT_FOUND`.

## Level 2 · Sensor

### `find_excursions`

| Param | Type | Notes |
|---|---|---|
| `fleet_id` / `reefer_id` / `shipment_id` | string | at least one |
| `hours` or `start` / `end` | | see the window rule |
| `min_minutes` | integer | drop shorter runs, default 2 |

```js
{
  scope: { fleetId: "FLEET-01" },
  window: { start: "...", end: "..." },
  events: [{
    eventType: "event:temperature_excursion",
    shipmentId: "SHP-102", reeferId: "RF-02",
    start: "2026-10-15T00:12:00Z", startLocal: "2026-10-15 09:12 KST",
    end: "2026-10-15T00:35:00Z",   endLocal: "2026-10-15 09:35 KST",
    measures: {                                             // names from ontology.md § 3
      direction: "above",
      consecutiveMinutes: 24,
      cumulativeMinutes: 24,
      peakTemp: 8.2,
      cause: "cause:compressor_stop"
    }
  }]
}
```

The tool calls `detect_temperature_excursion` with the resolved scope and window and returns its events unchanged, except for dropping runs shorter than `min_minutes`. The events are exactly the ones the engine sees. Excursions are computed per shipment, never across shipments, using each reading's own policy snapshot, and detection uses no `$lookup`.

Cargo and the policy range are not repeated on each event: they are on the shipment, and the LLM gets them from `get_shipment`.

### `find_energy_anomalies`

| Param | Type | Notes |
|---|---|---|
| `fleet_id` / `reefer_id` / `shipment_id` | string | at least one |

Returns one entry per shipment in scope with `measures.powerChangePct`: the last 24 h average power against the preceding 48 h, anchored on that shipment's latest reading (ontology.md § 3). The windows are part of the measure definition, so the tool takes no window parameters.

The tool reports the number only. It does not label a change as an anomaly: the threshold that makes a change actionable is a clause condition, and `evaluate_obligations` applies it.

### `get_fleet_overview`

`fleet_id` (required), `hours` or `start` / `end`. Per shipment: average, min, max, and last temperature; average power; door-open minutes; excursion count. The excursion count comes from `detect_temperature_excursion`, not a separate calculation.

### `get_reefer_timeline`

`reefer_id` (required), `hours` or `start` / `end`, `bucket_minutes` (default 5, raised automatically to keep at most 96 buckets). Per bucket: average, min, max temperature; average and min power; door open.

## Level 3 · Documents (stage 4)

### `search_knowledge`

| Param | Type | Notes |
|---|---|---|
| `query` | string | required |
| `shipment_id` | string | resolves `contractRef` and cargo class; restricts contract chunks to that version |
| `doc_type` | `contract` / `sop` / `manual` | optional |
| `k` | integer | default 4 |

```js
{
  contractRef: { contractId: "blueharbor-live-fish", version: "v1" },
  results: [{ chunkId: "...", source: "BlueHarbor ... § Article 6", text: "...", score: 0.82 }]
}
```

Contract chunks are filtered by `contractId` and `version` from the shipment. Without `shipment_id`, contract chunks are excluded, so the LLM cannot cite a random shipper's contract.

### `get_clause`

`clause_id` or `chunk_id`. Returns the exact chunk text and, if structured, the clause rule. Used when semantic search might miss a clause the evaluator already identified.

## Level 5 · Ontology

### `evaluate_obligations`

| Param | Type | Notes |
|---|---|---|
| `shipment_id` | string | required |

```js
{
  shipmentId: "SHP-102",
  contractRef: { contractId: "blueharbor-live-fish", version: "v1" },
  results: [
    { clauseId: "blueharbor-live-fish-v1-art6", kind: "obligation", status: "triggered",
      action: { type: "notify", party: "shipper.qa" },
      deadline: "2026-10-15T01:12:00Z", deadlineLocal: "2026-10-15 10:12 KST",
      basis: [{ measure: "consecutiveMinutes", op: "gte", required: 15, actual: 24, met: true }],
      chunkId: "blueharbor-live-fish-v1-article-6",
      follows: ["blueharbor-live-fish-v1-art7"] },          // via follow_references ($graphLookup)
    { clauseId: "blueharbor-live-fish-v1-art7", status: "triggered",
      basis: [{ dependsOn: "blueharbor-live-fish-v1-art6" }], ... },
    { clauseId: "blueharbor-live-fish-v1-art8", status: "not_triggered",
      basis: [{ measure: "cumulativeMinutes", op: "gte", required: 30, actual: 24, met: false }], ... }
  ],
  actions: [                                                  // one per triggered clause
    { action: "notify", party: "shipper.qa",
      deadline: "2026-10-15T01:12:00Z", deadlineLocal: "2026-10-15 10:12 KST",
      clauseId: "blueharbor-live-fish-v1-art6", kind: "obligation",
      evidence: "blueharbor-live-fish-v1-article-6", event: { ... } }, ...
  ]
}
```

The tool is `engine.evaluate_shipment` itself, registered under this name; only serialization is added. Internally the engine runs the `detector` of every event type the selected clauses need, including door openings and compressor stops, so no extra LLM tool is needed for them. `status` is one of `triggered`, `not_triggered`, `unresolved`. For `unresolved`, `basis` names what is missing (`{ missingData: "event:humidity_breach" }` or `{ missingMeasure: "..." }`). Steps and rules: [ontology.md § 5](./ontology.md#5-execution-enginepy).

## Ad Hoc

### `run_aggregation`

`collection`, `pipeline`. Dates are passed as `{ "$date": "<ISO 8601>" }`.

Guardrails, checked before the pipeline runs:
- `collection` is one of `readings`, `shipments`, `cargo_specs`.
- No `$out`, `$merge`, `$function`, `$accumulator`, `$where` at any depth.
- The `from` of `$lookup`, `$graphLookup`, and `$unionWith` must also be one of the allowed collections, so the pipeline cannot read `clauses`, `ontology`, `knowledge`, or the agent's checkpoint collections.
- `maxTimeMS` 5000; `$limit` 50 appended.

Returns `{ collection, results, truncated }`; `truncated` is true when the result hit the limit, so the LLM narrows the query instead of treating the list as complete. A violation is `QUERY_REJECTED`; a database error is `QUERY_FAILED`.

---

## Briefing Output

The system prompt makes the LLM answer a briefing in this order.

```text
<one-line summary>

Action Required        ← obligations with status "triggered"
· [Reefer / Shipment] <action> — <deadline> — <fact vs. condition>  [<source § clause>]

Needs Confirmation     ← clauses with status "unresolved"
· [Reefer / Shipment] <what could not be decided and what data is missing>  [<source>]

Incident Details       ← facts from level 2
· <reefer, cargo, policy range>: <start–end>, <minutes>, <peak>, <cause>   ← cargo and range from get_shipment

Suggestion             ← recommendations with status "triggered"
· [Reefer] <action>  [<source § section>]
```

Rules for the LLM:
- Numbers come only from tool results.
- Times are quoted from the `…Local` fields; the LLM never converts time zones.
- Every obligation, deadline, and recommendation cites a source returned by a tool.
- An `unresolved` clause goes under **Needs Confirmation**, never omitted and never reported as "no obligation."