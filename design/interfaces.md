# Interfaces

The LLM interacts with data only through these tools. Every tool is read-only.

## Tools by Level

| Level | Tool | Purpose | Backed by |
|---|---|---|---|
| 1 · Lookup | `get_shipment` | Shipment, reefer, route, policy, contract | `shipments` find |
| 2 · Sensor | `get_fleet_overview` | Per-shipment temperature, power, door summary | `readings` aggregation |
| 2 · Sensor | `find_excursions` | Out-of-range runs as events, with measures | `$setWindowFields` + `$shift` |
| 2 · Sensor | `find_energy_anomalies` | Power change vs. baseline | `$group` + `$cond` |
| 2 · Sensor | `get_reefer_timeline` | Time-bucketed temperature, power, door | `$dateTrunc` |
| 3 · Documents | `search_knowledge` | Relevant chunks from contracts, SOPs, manuals | `$vectorSearch` |
| 3 · Documents | `get_clause` | Exact clause text by `clauseId` or `chunkId` | `knowledge` find |
| 5 · Ontology | `evaluate_obligations` | Status of every applicable clause for a shipment | `clauses` + detection + `$graphLookup` |
| Ad hoc | `run_aggregation` | Numeric questions no fixed tool covers | Text-to-MQL, guarded |

Level 4 (briefing) has no tool of its own. The agent combines levels 2, 3, and 5.

---

## Common Rules

**Scope is required.** Sensor tools need at least one of `fleet_id`, `reefer_id`, or `shipment_id`. A call without scope is rejected, not widened.

**Time is bounded.** Fleet- and reefer-scoped calls take `hours` of 1–72 (default 24), or explicit `start`/`end` (ISO 8601 with offset) at most 72 hours apart; 72 matches the seeded history and is a setting, not a rule. Shipment-scoped calls (`shipment_id`, `evaluate_obligations`) use the shipment period instead, so cumulative measures cover the whole shipment.

**Times.** Inputs accept any offset. Outputs are ISO 8601 in UTC plus a `local` string in KST for display.

**Errors.** A tool never throws to the LLM. It returns:

```js
{ error: { code: "SCOPE_REQUIRED", message: "Provide fleet_id, reefer_id, or shipment_id." } }
```

| Code | When |
|---|---|
| `SCOPE_REQUIRED` | No scope on a sensor tool |
| `WINDOW_TOO_LARGE` | Window over 72 hours |
| `NOT_FOUND` | Unknown shipment, reefer, or clause |
| `EVIDENCE_UNAVAILABLE` | Search index not ready or no chunks for the contract version |
| `QUERY_REJECTED` | `run_aggregation` used a forbidden stage or collection |
| `QUERY_FAILED` | Database error; message included so the LLM can retry |

---

## Level 1 · Lookup

### `get_shipment`

| Param | Type | Notes |
|---|---|---|
| `shipment_id` | string | one of the two is required |
| `reefer_id` | string | returns the reefer's active shipment |

Returns the `shipments` document as stored.

## Level 2 · Sensor

### `find_excursions`

| Param | Type | Notes |
|---|---|---|
| `fleet_id` / `reefer_id` / `shipment_id` | string | at least one |
| `hours` | number | default 24 |
| `min_minutes` | integer | drop shorter runs, default 2 |

```js
{
  scope: { fleetId: "FLEET-01" },
  window: { start: "...", end: "..." },
  events: [{
    eventType: "event:temperature_excursion",
    shipmentId: "SHP-102", reeferId: "RF-02", cargoType: "cargo:live_fish",   // from shipments
    policy: { minC: 0, maxC: 4, policyVersion: "SHP-102-policy-v1" },
    start: "2026-10-15T00:12:00Z", end: "2026-10-15T00:35:00Z",
    measures: {                              // names from ontology.md § 3
      direction: "above",
      consecutiveMinutes: 24,
      cumulativeMinutes: 24,
      peakTemp: 8.2,
      cause: "cause:compressor_stop"
    }
  }]
}
```

Excursions are computed per shipment, never across shipments, using each reading's own policy snapshot. Detection uses no `$lookup`. After detection, the tool adds `cargoType` from `shipments` for the few shipments in the result, so the LLM sees cargo without it being stored in `meta`.

### `find_energy_anomalies`

| Param | Type | Notes |
|---|---|---|
| `fleet_id` / `reefer_id` | string | at least one |
| `hours` | number | recent window, default 24 |
| `baseline_hours` | number | default 48 |

Returns one entry per reefer with `measures.powerChangePct`, and `eventType: "event:power_anomaly"` when the change is 20% or more.

### `get_fleet_overview`

`fleet_id` (required), `hours`. Per shipment: average, min, max, and last temperature; average power; door-open minutes; excursion count.

### `get_reefer_timeline`

`reefer_id`, `start`, `end` (required), `bucket_minutes` (default 5, raised automatically to keep at most 96 buckets). Per bucket: average, min, max temperature; average and min power; door open.

## Level 3 · Documents

### `search_knowledge`

| Param | Type | Notes |
|---|---|---|
| `query` | string | required |
| `shipment_id` | string | resolves `contractRef` and cargo class; restricts contract chunks to that version |
| `doc_type` | `contract` / `sop` / `manual` | optional |
| `k` | integer | default 4 |

```js
{
  engine: "atlas-vector-search",             // or "local-cosine-fallback"
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
      deadline: "2026-10-15T01:12:00Z",
      basis: [{ measure: "consecutiveMinutes", op: "gte", required: 15, actual: 24 }],
      chunkId: "blueharbor-live-fish-v1-article-6" },
    { clauseId: "blueharbor-live-fish-v1-art7", status: "triggered",
      basis: [{ dependsOn: "blueharbor-live-fish-v1-art6" }], ... },
    { clauseId: "blueharbor-live-fish-v1-art8", status: "not_triggered",
      basis: [{ measure: "cumulativeMinutes", op: "gte", required: 30, actual: 24 }], ... }
  ]
}
```

Internally it detects every event type the selected clauses need, including door openings and compressor stops, by running each event class's `detect` definition from the `ontology` collection; no extra LLM tool is needed for them. `status` is one of `triggered`, `not_triggered`, `unresolved`. For `unresolved`, `basis` names what is missing (for example `{ missingField: "humidityPct" }`). Evaluation rules: [ontology.md § 5](./ontology.md#5-evaluation).

## Ad Hoc

### `run_aggregation`

`collection` (`readings`, `shipments`, `cargo_specs`), `pipeline`. Guardrails: no `$out`, `$merge`, `$function`, `$accumulator`; `maxTimeMS` 5000; `$limit` 50 appended. Dates are passed as `{ "$date": "<ISO 8601>" }`.

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
· <reefer, cargo, range>: <start–end>, <minutes>, <peak>, <cause>

Suggestion             ← recommendations with status "triggered"
· [Reefer] <action>  [<source § section>]
```

Rules for the LLM:
- Numbers come only from tool results.
- Every obligation, deadline, and recommendation cites a source returned by a tool.
- An `unresolved` clause goes under **Needs Confirmation**, never omitted and never reported as "no obligation."
