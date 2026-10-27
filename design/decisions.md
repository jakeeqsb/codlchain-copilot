# Design Decisions

## 1. Embed the temperature policy in each reading

Each reading carries its shipment's `minC`, `maxC`, and `policyVersion` in `meta`.

- **Why:** Detection compares a reading with its own bounds, with no join. Past readings keep the thresholds they were recorded under, even if cargo defaults change.
- **Cost:** Small. Readings with the same `meta` share a Time Series bucket, so the snapshot is stored once per bucket.
- **Constraint:** The policy is fixed for the whole shipment and validated before insertion.
- **Note:** MongoDB recommends putting only filter fields in `meta`. The policy is an exception on purpose: it is one-to-one with `shipmentId`, so it never creates extra buckets. Everything else that is not a filter (`cargoType`, `route`) stays out of `meta`.

## 2. Compute incidents at query time

Excursions and anomalies are calculated by aggregation when asked. No incident collection, no background detector.

- **Why:**
  - **Always matches the raw data.** Incidents are derived from readings on every query, so they cannot go stale, duplicate, or drift from the source. Late or corrected readings are reflected automatically.
  - **Rule changes apply instantly.** If the continuity rule or a minimum duration changes, the next query uses it for all history. A stored incident collection would need to be recomputed and reconciled.
  - **Fewer moving parts.** No background job, no processing checkpoints, no "did the detector miss a run?" failure mode.
  - **One question, any window.** The same readings answer "last 24 hours," "this shipment," or "excursions of at least 10 minutes" without precomputing each variant.
  - **Cheap at this scale.** Five reefers over 72 hours is a few dozen buckets per query.
- **Cost:** The work is repeated on every briefing, and there is no record of *when* an incident was first detected. Deadlines are therefore anchored on event times (`eventStart`, `eventEnd`), not on detection time.
- **Revisit when:** Briefings become slow or continuous alerting is needed. Then store incidents while keeping raw readings.

## 3. Turn contract clauses into rules

Each clause is stored as a rule (trigger conditions, action, deadline, dependencies) linked to its source text. The database decides whether it applies; the LLM explains the result.

- **Why:** Same input, same decision. Dependent clauses are found by following references, not by search luck.
- **Missing data:** If the data a clause needs is not collected, the result is "unresolved," never "no obligation."
- **Limit:** Conditions are AND-only on a fixed list of measures. Clauses that do not fit stay as text and are cited, not decided.

## 4. Keep the graph in MongoDB, for now

Relations are existing fields; `$graphLookup` is used only for clause dependencies (1–2 hops).

- **Why:** The graph is small, shallow, and always starts from a shipment or incident.

  | Relationship | Shape | Handled by |
  |---|---|---|
  | Shipment → contract version | 1 hop | Field `shipments.contractRef` |
  | Cargo class → parent classes | Fixed, rarely changes | Precomputed `ancestors`, matched with `$in` |
  | Clause → clause it depends on | 1–2 hops | `$graphLookup` on `clauses.references` |

- **Where MongoDB stops:** `$graphLookup` follows one connection field recursively within a per-stage memory limit. It has no shortest-path or weighted-path queries, no variable-length pattern matching across several edge types (e.g. Reefer → Compressor model → Part batch → Supplier → other Reefers), no graph algorithms (centrality, community detection), and no RDF/OWL-style reasoning. As depth, fan-out, and edge types grow, each hop becomes another index lookup inside an aggregation stage.
- **Revisit when:** Questions are about the network itself, such as shared part batches across shipments or the most common failure point among depots. Then split the work:
  - MongoDB stays the system of record: sensor history, shipments, documents, embeddings, per-shipment rule evaluation.
  - A graph database holds a projection of relationships (equipment, parts, suppliers, depots) for multi-hop paths and analytics.
  - Shared IDs connect them: the graph answers *which entities*, MongoDB answers *what we know about them*.

## 5. Keep all four kinds of data in one MongoDB cluster

Sensor facts, shipment context, contract text, and rules live in one database.

- **Document model.** A shipment embeds route, policy, and contract reference (read whole, no joins). Readings copy only the policy fields detection needs (Extended Reference pattern), instead of an effective-dated policy table and a temporal join. Clauses of different shapes share one collection (polymorphic documents). New fields need no migration (flexible schema). The MVP does not add JSON Schema validation: seed data is written by the project itself, and readings are checked at ingestion.
- **Time Series.** Buckets and column compression (delta, delta-of-delta, zstd) reduce storage, index size, and cache use. `meta` is a structured document, queried by subfield. Excursions are detected with `$setWindowFields` and `$shift` on the time series itself. Retention is `expireAfterSeconds`; sharding on `meta` fields is available later.
- **Vector Search.** `$vectorSearch` filters by the `contractId` and `version` read from the shipment, so no metadata is copied into a separate store.
- **Accepted limits.** Time Series collections are append-oriented and do not support Change Streams, Database Triggers, or schema validation. Readings are never edited and are validated at ingestion, so these limits do not block the MVP.

