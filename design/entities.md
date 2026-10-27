# Entities and Collections

Six collections, in three groups.

| Group | Collection | Type | Role |
|---|---|---|---|
| Operations | `shipments` | Regular | Source of truth for each shipment: reefer, cargo, policy, contract |
| Operations | `cargo_specs` | Regular | Default temperature ranges by cargo type (used at shipment setup only) |
| Sensor | `readings` | Time Series | Minute-level measurements with a policy snapshot in `meta` |
| Knowledge | `knowledge` | Regular + Vector Search index | Document chunks and embeddings (RAG) |
| Knowledge | `clauses` | Regular | Contract and SOP clauses as structured rules |
| Knowledge | `ontology` | Regular | Class hierarchy, relations, and measure vocabulary |

```text
cargo_specs ──(setup only)──▶ shipments ──(policy copied)──▶ readings.meta
                                 │
                                 └── contractRef ──▶ clauses ──chunkId──▶ knowledge
                                                        │
                                                   ontology (classes, measures)
```

`shipments` is the hub. Detection reads only `readings`. Contract assessment starts from `shipments.contractRef`.

---

## shipments

```js
{
  shipmentId: "SHP-102",                  // globally unique
  fleetId: "FLEET-01",
  reeferId: "RF-02",                      // globally unique
  shipperId: "blueharbor",
  cargoType: "cargo:live_fish",           // the ontology class id itself
  route: { origin: "Harbor City", destination: "Central Seafood Market" },
  temperaturePolicy: {
    minC: 0, maxC: 4,
    sourceSpecVersion: "live-fish-v1",    // which default it came from
    policyVersion: "SHP-102-policy-v1"    // identity of the resolved policy
  },
  contractRef: { contractId: "blueharbor-live-fish", version: "v1" },
  departedAt: ISODate("2026-10-12T00:00:00Z"),   // shipment period starts
  deliveredAt: null                               // null while in transit
}
```

**Rules**
- The policy is resolved and validated once, when the shipment is created, and stays fixed until delivery.
- The policy range must match the applicable contract. It is set from data, never from vector search. In the MVP, every seeded policy matches both its cargo default and its contract; which one wins when they differ is out of scope.
- One shipment has exactly one contract version.
- `cargoType` stores the ontology class id itself (`cargo:live_fish`), everywhere it appears, so it can be used directly as a reference without adding a prefix in code.
- The shipment period runs from `departedAt` to `deliveredAt`, or to now while `deliveredAt` is null. Contract measures such as cumulative excursion time are counted over this period. There is no separate `status` field; it would only duplicate what the dates already say.

## cargo_specs

```js
{ cargoType: "cargo:live_fish", specVersion: "live-fish-v1",
  temperaturePolicy: { minC: 0, maxC: 4 } }
```

A versioned master of defaults. It is read when a shipment is created. Changing it affects future shipments only; existing readings keep their snapshot.

## readings (Time Series)

```js
// createCollection("readings", { timeseries: { timeField: "ts", metaField: "meta", granularity: "minutes" } })
{
  ts: ISODate("2026-10-15T00:12:00Z"),
  meta: {                                  // stable for the whole shipment
    fleetId: "FLEET-01",
    reeferId: "RF-02",
    shipmentId: "SHP-102",
    temperaturePolicy: { minC: 0, maxC: 4, policyVersion: "SHP-102-policy-v1" }
  },
  temp: 3.42,                              // °C
  powerKw: 2.13,                           // compressor power
  doorOpen: false
}
```

**Why the policy is in `meta`**
Detection compares each reading with its own bounds, without joining `cargo_specs` or `shipments`. Readings with identical `meta` share a bucket, so the snapshot is stored once per bucket, not once per reading.

**Rules**
- `meta` holds the identifiers queries filter on (`fleetId`, `reeferId`, `shipmentId`) and the shipment's fixed policy. Nothing else: `cargoType` and `route` live in `shipments`; measurements stay outside `meta`.
- Query `meta` by subfield (`meta.reeferId`), never by the whole object.
- Before insertion, check that the policy exists, `minC <= maxC`, and `policyVersion` matches the shipment. Reject the reading otherwise; never fall back to the latest default.
- A value equal to `minC` or `maxC` is in range.
- Indexes: `{ meta.fleetId: 1, ts: 1 }` for fleet briefings and `{ meta.reeferId: 1, ts: 1 }` for reefer timelines. The automatic `{ meta, ts }` index (6.3+) covers the whole `meta` object and only serves equality on it, so subfield queries need these.
- No humidity sensor in the MVP. This is deliberate: it is used to demonstrate an unresolved assessment (see ontology.md).

## knowledge

```js
{
  chunkId: "blueharbor-live-fish-v1-article-6",
  docId: "blueharbor-live-fish",
  docType: "contract",                     // contract | sop | manual
  contractId: "blueharbor-live-fish",      // contracts only
  version: "v1",
  appliesTo: ["cargo:live_fish"],          // ontology classes; SOPs may use a parent class
  title: "BlueHarbor Fisheries Live Fish Transport Contract",
  section: "Article 6: Notification Upon Temperature Excursion",
  text: "If the internal temperature exceeds 4°C for at least 15 consecutive minutes ...",
  embedding: [ ... ]
}
```

**Rules**
- One chunk per article or section, so a citation points to one clause.
- No shipment IDs on chunks. Which contract applies is known only by `shipments.contractRef`, so one contract version can serve many shipments unchanged.
- Vector Search index filter fields: `docType`, `contractId`, `version`, `appliesTo`.

## clauses

The structured form of a clause. Detailed format and evaluation rules: [ontology.md](./ontology.md).

```js
{
  clauseId: "blueharbor-live-fish-v1-art6",
  contractId: "blueharbor-live-fish", version: "v1",
  chunkId: "blueharbor-live-fish-v1-article-6",   // the source text
  kind: "obligation",
  appliesTo: ["cargo:live_fish"],
  trigger: {
    eventType: "event:temperature_excursion",
    allOf: [ { measure: "consecutiveMinutes", op: "gte", value: 15 } ]
  },
  action:   { type: "notify", party: "shipper.qa" },
  deadline: { anchor: "eventStart", offsetMinutes: 60 },
  references: [],
  review: { status: "approved" }
}
```

**Rules**
- Every clause points to its source chunk (`chunkId`). A decision can always be traced back to the original sentence.
- Only `review.status: "approved"` clauses are used in assessment.
- SOP and manual clauses have no `contractId`; they apply through `appliesTo`.

## ontology

Definitions, not data. One document per class, relation, or measure. Kept as a collection, not code constants, so the vocabulary lives next to the data it describes and tools and the LLM read the same definitions.

```js
{ _id: "cargo:live_fish", kind: "class", parent: "cargo:live_seafood",
  ancestors: ["cargo:live_seafood", "cargo:seafood"] }

{ _id: "rel:governedBy", kind: "relation",
  domain: "Shipment", range: "ContractVersion", storedAt: "shipments.contractRef" }

{ _id: "measure:consecutiveMinutes", kind: "measure",
  appliesTo: "event:temperature_excursion", unit: "min" }
```

Full lists: [ontology.md](./ontology.md).

---

## Seeded Data

| Reefer | Shipment | Shipper | Cargo | Range | Contract |
|---|---|---|---|---|---|
| RF-01 | SHP-101 | BlueHarbor Fisheries | `cargo:live_fish` | 0 – 4°C | `blueharbor-live-fish` v1 |
| RF-02 | SHP-102 | BlueHarbor Fisheries | `cargo:live_fish` | 0 – 4°C | `blueharbor-live-fish` v1 |
| RF-03 | SHP-103 | OceanPeak Tuna Trading | `cargo:frozen_tuna` | −60 – −50°C | `oceanpeak-tuna` v1 |
| RF-04 | SHP-104 | CoastalFresh Seafood | `cargo:frozen_shrimp` | −25 – −18°C | `coastalfresh-shrimp` v1 |
| RF-05 | SHP-105 | TidalPearl Oysters | `cargo:oyster` | 1 – 5°C | `tidalpearl-oyster` v1 |

All in `FLEET-01`, all departed when the seed starts (72 hours before it runs) and still in transit. SHP-101 and SHP-102 share one contract version, which shows chunk reuse across shipments. All companies and documents are fictional.
