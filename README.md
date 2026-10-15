# ColdChain Copilot (`coldchain-copilot`)

MongoDB Solo Project Assignment

An operational agent that reads cold-chain IoT data and uses contracts and manuals to explain what needs to be done now.

MongoDB Time Series × Vector Search (RAG) × LLM Tool Calling MVP demo

```text
> Brief me on today's data.
  ↳ find_excursions({"hours": 24})
  ↳ find_energy_anomalies({"hours": 24})
  ↳ get_reefer_timeline({"reefer_id": "RF-02", ...})
  ↳ search_knowledge({"query": "live fish notification obligation above 4 degrees", "shipment_id": "SHP-102", "doc_type": "contract"})
  ↳ search_knowledge({"query": "refrigeration shutdown response procedure", "doc_type": "sop"})
  ↳ ...

Over the last 24 hours, there were two temperature excursions and one sign of equipment abnormality. Three cases have contractual notification deadlines.

Action Required
· [RF-02 / SHP-102] Notify BlueHarbor Fisheries' quality assurance team by 10:12 (excursion start at 09:12 + 1 hour).
  Evidence: 24 consecutive minutes outside the permitted range ≥ 15 minutes [BlueHarbor Fisheries Live Fish Transport Contract § Article 6]
· [RF-02 / SHP-102] Do not unload at the destination before a quality inspection with the shipper present [same contract § Article 7].
· [RF-03 / SHP-103] Notify OceanPeak Tuna Trading's logistics team within 30 minutes and arrange a third-party flesh-color inspection [OceanPeak Tuna Trading contract § Articles 5 and 6].
· [RF-04 / SHP-104] Notify the shipper of signs of equipment abnormality and submit a backup equipment plan within 24 hours [CoastalFresh Seafood contract § Article 5].

Incident Details
· RF-02, live fish (0–4°C): 09:12–09:35, 24 minutes, peak 8.2°C. Refrigeration power was 0 kW during the same period → suspected refrigeration shutdown.
· RF-03, frozen tuna (-60 to -50°C): 11:20–11:37, 18 minutes, peak -42°C. Coincides with 15 minutes of door opening (exceeds the 10-minute unloading limit).
· RF-04, frozen shrimp: temperature normal; power consumption +30% above baseline.

Suggestion
· RF-02: Inspect the compressor and electrical system after the trip, before reassignment [Maintenance Manual § 4].
· RF-04: Check for refrigerant leaks, condenser contamination, and door gasket issues; schedule maintenance within 48 hours [Maintenance Manual § 2].
· RF-03: Record the unloading SOP violation and retrain the work crew [Excursion Response SOP § 3].
```

## 1. What Is This Project?

In refrigerated and frozen transport (cold chain), each reefer container produces temperature, refrigeration power, and door-status readings every minute. A simple rule can detect whether a temperature threshold was exceeded, but the real operational work comes afterward:

* Why did the excursion happen: refrigeration shutdown or an open door?
* What does this shipper's contract require, and by when: notification, quality inspection, or compensation conditions?
* What do internal SOPs and maintenance manuals recommend?
* Which equipment is likely to develop a problem, even if no incident has occurred yet?

The information needed for these decisions is split across two sources: numbers in sensor data and rules in contracts and manuals. ColdChain Copilot brings them together so an operator can say “Brief me on today's data” and receive both what happened (facts) and what to do next (Action Required / Suggestion), with supporting evidence.

### How It Works: Detection → Evidence Retrieval → Decision

```text
 User question ("Brief me on today's data")
     │
     ▼
 ┌─────────┐  Decides which tools to call and with which parameters
 │   LLM   │ ──────────────────────────────────────────────────────┐
 └─────────┘                                                      │
     ▲                                                            ▼
     │                         ┌────────────────────────────────────────────┐
     │                         │                 MongoDB                    │
     │  ① Detection (facts)    │  readings (Time Series + policy)       │
     │  Aggregation summaries ◀│  → Excursions, power anomalies, timelines │
     │                         │                                            │
     │  ② Evidence (RAG)       │  knowledge (document chunks + embeddings)  │
     │  Relevant clauses ◀─────│  → $vectorSearch + contract/version filters  │
     │                         └────────────────────────────────────────────┘
     │
 ③ Decision: Compare the facts (24 minutes) with clause conditions
    (at least 15 consecutive minutes).
     → Action Required (obligation, deadline, evidence) / Suggestion
```

* Detection is performed by aggregation. Instead of tens of thousands of sensor readings, the LLM receives summaries such as “two excursions, with start time, end time, and peak temperature.” This avoids errors from asking the LLM to count or compare raw values and produces consistent numbers for the same query over the same data.
* Evidence is retrieved through vector search, with exact metadata/ID retrieval where required for clause coverage. Contract retrieval is scoped by shipment, cargo type, and the applicable contract ID/version resolved from `shipments` to prevent cross-shipper or cross-version evidence.
* Decisions are made by the LLM. It compares sensor facts with clause conditions and cites only retrieved clauses. It does not invent obligations that were not retrieved.
* The LLM repeats this process as needed through an agentic loop.

### MVP Design Decision and Assertions

Decision ID: MVP-001 — Query-Time Detection with Embedded Policy Snapshots

Status: Accepted for the MVP; implementation and validation are pending.

For this MVP, we choose a hybrid document model: embed the resolved numerical temperature policy in Time Series readings, keep versioned contracts and manuals separate, and calculate incidents at query time. This is a workload-specific decision, not a claim that embedding is universally better than normalization.

#### Model and Responsibilities

| Collection | Responsibility | Usage |
|---|---|---|
| `cargo_specs` | Versioned default temperature specifications by cargo type | Resolve defaults during shipment setup; not joined during detection |
| `shipments` | Authoritative fleet/reefer assignment, route, resolved policy, and applicable contract ID/version | Supply stable ingestion context and scope contract retrieval |
| `readings` | Raw sensor measurements with stable fleet/reefer/shipment IDs and a compact policy snapshot in `meta.temperaturePolicy` | Calculate excursions, equipment indicators, and timelines through ordinary aggregation |
| `knowledge` | Versioned contract, SOP, and manual chunks with text, metadata, and embeddings | Retrieve evidence through Vector Search and, where required, exact metadata/ID queries |

Excursions are query results in this MVP, not a persisted incident collection. Search is not used to determine whether a sensor incident exists. The agent combines computed facts with applicable evidence when contract or procedure assessment is needed.

#### Assumptions Under Which We Choose This Model

* The initial workload is five reefers in one fleet with one-minute measurements over a 72-hour history. Interactive briefings and investigation take priority over continuous alerting.
* The MVP deployment is unsharded. Sharding and dedicated Search Node demonstrations are not required; the model anticipates future scale-out without claiming validated sharded performance.
* Each shipment has one validated temperature policy that remains fixed throughout that shipment. Its bounds and contract applicability are resolved before readings are generated or ingested.
* The primary detection workload spans multiple shipments with potentially different thresholds, making local comparisons against each reading's snapshot convenient.
* Demo contracts and manuals are fictional, versioned, and loaded before assessment. Documents must be available to the retrieval path before the agent is allowed to rely on them.
* The MVP supports evidence-backed action recommendations, not autonomous external notifications or other consequential execution.

#### Design Assertions and Validation Criteria

These are required properties to test, not claims of completed verification.

| Assertion | Required Validation |
|---|---|
| A1 — Detection does not depend on contract retrieval or a cargo-specification join. | Inspect detection pipelines for absence of `$lookup` against `cargo_specs`; verify sensor facts remain available when knowledge retrieval fails. |
| A2 — Historical readings retain their originally assigned thresholds. | Change a cargo default for a future shipment and verify prior snapshots and prior detection results remain unchanged for the same data and query window. |
| A3 — Every reading uses the shipment's resolved policy. | Validate numeric bounds, `minC <= maxC`, and policy identity before insertion; reject missing or inconsistent snapshots rather than substituting the latest default. |
| A4 — Incident calculations are deterministic and scoped correctly. | Test boundary equality, known consecutive intervals, shipment separation, query-window edges, and missing/duplicate measurements. Define duration and continuity rules in the design specs before claiming correctness. |
| A5 — Contract assessment uses the applicable evidence version. | Resolve the contract ID/version from `shipments`, enforce retrieval filters, and test that another shipper's or another version's clauses cannot support a decision. Missing evidence must produce an unresolved assessment, not “no obligation.” |
| A6 — Contract retrieval is not restricted to temperature excursions. | Test equipment anomalies with normal temperatures, relevant door-opening events, and direct contract questions. Use exact clause retrieval where semantic top-k results cannot establish required coverage. |
| A7 — Detection is bounded by explicit operational scope and time. | Test fleet, reefer, and shipment filters; validate fleet consistency between shipments and readings; reject unbounded sensor queries. Define maximum windows in the interface specs. Fleet scope is not a substitute for authorization. |
| A8 — Contract chunks are reusable across shipments. | Assign an existing contract version to a new shipment without modifying document chunks; verify retrieval resolves that shipment's `contractRef` correctly. |

#### Rationale and Accepted Tradeoffs

We prioritize a simple, explainable read path and preservation of shipment-time thresholds. Query-time aggregation avoids introducing a background detector, processing checkpoints, and persisted incident reconciliation into the initial MVP. Separate document storage keeps contractual obligations and procedural guidance out of sensor metadata.

We accept the ingestion-side responsibility for policy consistency, the difficulty of correcting invalid historical snapshots, and repeated computation during briefings. Only stable numeric bounds and policy provenance belong in `meta`; full clauses and volatile assessment details do not.

Search index freshness is a separate concern from ordinary sensor queries. Contract versions must be pinned, and unavailable or incomplete evidence must block a definitive contractual assessment. Dedicated Search Nodes are an optional workload-isolation choice, not a correctness dependency or a guarantee of immediate search visibility.

Removing a detection-time join is expected to simplify execution, but no latency, throughput, or storage improvement is asserted without measurement. Validate the chosen model on the seeded workload and inspect aggregation execution plans; use a comparable lookup-based baseline if making comparative performance claims.

#### Conditions for Revisiting the Decision

Revisit this model if measured query latency becomes unacceptable, repeated detection becomes a material load, or continuous alerting is required. At that point, consider materializing incidents while retaining raw readings. Frequent mid-shipment policy changes or mandatory retrospective corrections would also require revisiting policy versioning and snapshot handling.

## 2. Why MongoDB?

To answer correctly, the agent must connect sensor time series, operational data such as cargo specifications, and contracts/manuals with their embeddings. MongoDB's primary advantage is handling all three with one document model, one query language, and one cluster.

### 2.1 Document Model: Data and Context Together

```js
// cargo_specs (regular collection: versioned default specifications)
{ cargoType: "live_fish", specVersion: "live-fish-v1",
  temperaturePolicy: { minC: 0, maxC: 4 } }

// shipments (regular collection: authoritative shipment context)
{
  shipmentId: "SHP-102",
  fleetId: "FLEET-01",
  reeferId: "RF-02",
  cargoType: "live_fish",
  route: {
    origin: "Harbor City",
    destination: "Central Seafood Market"
  },
  temperaturePolicy: {
    minC: 0, maxC: 4,
    sourceSpecVersion: "live-fish-v1",
    policyVersion: "SHP-102-policy-v1"
  },
  contractRef: { contractId: "blueharbor-live-fish", version: "v1" }
}

// readings (Time Series: shipment-specific policy snapshot)
{
  ts: ISODate("2026-10-15T00:12:00Z"),
  meta: {
    fleetId: "FLEET-01",
    reeferId: "RF-02",
    shipmentId: "SHP-102",
    cargoType: "live_fish",
    temperaturePolicy: {
      minC: 0,
      maxC: 4,
      policyVersion: "SHP-102-policy-v1"
    }
  },
  temp: 3.42,
  powerKw: 2.13,
  doorOpen: false
}

// knowledge (contract chunk)
{
  chunkId: "blueharbor-live-fish-v1-article-6",
  title: "BlueHarbor Fisheries Live Fish Transport Contract",
  section: "Article 6: Notification Upon Temperature Excursion",
  docType: "contract",
  contractId: "blueharbor-live-fish",
  version: "v1",
  cargoTypes: ["live_fish"],
  text: "If the internal temperature exceeds 4°C for at least 15 consecutive minutes ...",
  embedding: [0.012, -0.034, ...]
}
```

* Sensor measurements, equipment/cargo/shipment context, and the applicable temperature-policy snapshot live in the same document. Detection reads `readings` directly without a `$lookup` against `cargo_specs`.
* Document chunks store text, embeddings, document identity, version, and cargo applicability together. Resolve `shipments.contractRef` before retrieving contract evidence. Chunks do not contain growing shipment-ID arrays: multiple shipments reuse the same contract version without modifying its chunks.
* New sensor types (humidity, GPS, vibration) or document metadata can be accommodated by adding fields without a schema migration.
* JSON documents are also easy for the LLM to read, simplifying schema descriptions for Text-to-MQL.

### 2.1.1 Cargo Specifications: Manage Defaults, Embed the Applicable Policy

* `cargo_specs` remains the master collection for versioned default temperature ranges by cargo type. It is used when preparing a shipment, not during detection queries.
* At shipment setup, resolve and validate the applicable temperature policy from the cargo default and any shipment-specific requirements. Store the resolved policy and contract ID/version in `shipments`. The numeric range must agree with the applicable contract; vector retrieval does not automatically configure sensor thresholds.
* Copy the resolved policy into `readings.meta.temperaturePolicy` when generating or ingesting readings. Store the actual bounds (`minC`, `maxC`) alongside the resolved policy identity (`policyVersion`), rather than only a reference that requires another lookup. Keep default-spec provenance (`sourceSpecVersion`) in `shipments`.
* Keep this policy stable throughout a shipment in the MVP. Updating `cargo_specs` affects future shipment setup, not historical reading snapshots. Mid-shipment policy changes and retrospective reclassification are outside the current scope.
* `meta` holds stable shipment context and policy; changing measurements (`temp`, `powerKw`, `doorOpen`) remain outside `meta`.
* The embedded policy determines whether a reading is outside the permitted range. Notification deadlines, inspection obligations, and recommended responses remain in contracts, SOPs, and manuals retrieved from `knowledge`.

For example, detection compares each reading with its own policy snapshot:

```js
{
  $set: {
    outOfRange: {
      $or: [
        { $lt: ["$temp", "$meta.temperaturePolicy.minC"] },
        { $gt: ["$temp", "$meta.temperaturePolicy.maxC"] }
      ]
    }
  }
}
```

A temperature equal to either bound is within range. Validate policy presence and numeric bounds before inserting readings; do not silently substitute the latest cargo default for missing historical policies.

### 2.2 Time Series Collections: Specialized Storage, General-Purpose Queries

* With `timeField` and `metaField` configured, MongoDB groups consecutive measurements with matching metadata into internal buckets and compresses them, reducing storage and I/O for write-heavy sensor data.
* Queries use the same MQL and aggregation framework as regular collections. No separate time-series query language is required.
* Detection is implemented through aggregation stages:
  * Group consecutive out-of-range readings into incidents: `$setWindowFields` + `$shift` (gaps and islands).
  * Build time-bucketed trends: `$dateTrunc`.
  * Compare readings with embedded policy bounds: `$lt` / `$gt` against `meta.temperaturePolicy.minC` and `meta.temperaturePolicy.maxC`, without `$lookup`.

### 2.3 Vector Search: Document Retrieval Beside Operational Data

* Contract, SOP, and manual chunks and their embeddings are stored in the same database as sensor data and retrieved using `$vectorSearch`.
* Vector similarity search and metadata filters (`cargoTypes`, `docType`, `contractId`, `version`) are applied in one stage, excluding another shipper's clauses even when their wording is similar.
* Because `$vectorSearch` is an aggregation stage, detection and evidence retrieval use the same query interface, driver, and connection.

### 2.4 Aggregation as the Agent's Calculation Engine

LLMs can make calculation errors. The tools in this project therefore use aggregation pipelines for detection and `$vectorSearch` for evidence retrieval. Filtering, policy comparisons, grouping, window operations, and vector search happen in the database; application code passes the results to the LLM. For numerical questions not covered by fixed tools, the LLM generates a pipeline through Text-to-MQL and submits it to the same engine.

### 2.5 Scale-Aware Modeling Without a Sharding Demo

The MVP remains unsharded. Scale awareness is expressed through document boundaries, stable identifiers, bounded queries, and explicit validation criteria—not through an additional deployment requirement.

#### Stable Metadata and Query Scope

* `readings.meta` contains `fleetId`, `reeferId`, `shipmentId`, `cargoType`, and a compact, fixed temperature-policy snapshot. Fleet assignment remains stable for a shipment in the MVP. Routes and other nonessential transport details live in `shipments`; volatile measurements remain outside `meta`.
* Define `reeferId` and `shipmentId` as globally unique identifiers. Use `fleetId` as an operational query scope, not as an authorization mechanism.
* Fleet briefings specify a fleet and bounded time window. Reefer investigations specify a reefer and time window; incident calculations separate reefer/shipment series. A fleet-wide query can still require multiple shards after future sharding.

#### Candidate Shard Key and Independent Index Design

If ingestion distribution and reefer-scoped access justify it, evaluate this candidate for `readings`:

```js
// Future candidate only; no sharding command is part of the MVP.
{ "meta.reeferId": "hashed" }
```

This is not a finalized shard key. Evaluate distinct active reefers, per-reefer write skew, retained data distribution, and the balance between reefer, shipment, and fleet queries. A single hot reefer is not split across shards merely by hashing its ID. A fleet-only filter does not guarantee shard targeting with this key. Avoid time-based shard-key assumptions; support and restrictions must be checked against the deployment's MongoDB version.

Shard routing and query indexes are separate design concerns. Evaluate these query-index candidates on the unsharded workload and inspect execution plans before adopting them:

```js
{ "meta.reeferId": 1, ts: 1 }
{ "meta.fleetId": 1, ts: 1 }
// Evaluate a shipment/time index only if shipment queries justify it.
```

These are proposed patterns, not indexes asserted to exist. Keep indexes that provide measured benefit; unnecessary indexes add ingestion and storage overhead. For any future sharded deployment, inspect where sorting and window calculations execute and measure fan-out and merge costs. More shards do not automatically make fleet briefings faster.

#### Collection-Specific Scaling and Search Freshness

* `readings` is the primary future sharding candidate because it carries the high-volume sensor history. Do not shard `cargo_specs`, `shipments`, or `knowledge` merely because sensor volume grows; evaluate each collection independently.
* Scale database ingestion/aggregation and document Search capacity independently. Dedicated Search Nodes are optional for this MVP. Contract ID/version filters enforce evidence applicability; they must not be treated as a guarantee of Search shard targeting.
* Ordinary sensor queries remain outside the Search index path. Index synchronization lag and document version availability must be handled by the evidence-retrieval path, with unresolved assessments when required evidence is unavailable.
* Define raw-data retention separately from scalability. Time Series expiration can bound retained history; no persisted incident or summary collection is required in this MVP.

#### Evidence Required Before Scale-Out Claims

Measure ingestion throughput, concurrent briefing p95 latency, examined data/buckets, sort/window resource usage, and retained storage on representative workloads. Future sharded tests must also measure per-shard skew and query fan-out; Search tests must measure retrieval latency, index freshness, and cost. If repeated query-time detection becomes the bottleneck, revisit materialized incidents rather than assuming additional shards are sufficient.

MongoDB references: [Time Series best practices](https://www.mongodb.com/docs/manual/core/timeseries/timeseries-best-practices/), [sharding a Time Series collection](https://www.mongodb.com/docs/manual/core/timeseries/timeseries-shard-collection/), and [Vector Search deployment options](https://www.mongodb.com/docs/vector-search/deployment/deployment-options/).

### 2.6 Architecture: One Platform

```text
 Traditional setup                         MongoDB setup
 ┌────────────┐ ┌──────────┐ ┌──────────┐   ┌─────────────────────────────────┐
 │ Time-series│ │Operational│ │ Vector DB│   │             MongoDB             │
 │ DB (sensor)│ │ RDB      │ │(contracts)│   │ readings       cargo_specs       │
 └─────┬──────┘ └────┬─────┘ └────┬─────┘   │ (Time Series)  (regular)        │
       │             │            │         │ knowledge (chunks + embeddings) │
       └─ ETL / metadata sync / app joins ─┘│ → aggregation + $vectorSearch   │
                     │                      └────────────────┬────────────────┘
                Application                                  │ MQL
                     │                                  Application
                    LLM                                      │
                                                            LLM
```

* Co-locating sensor data, operational data, and embeddings avoids cross-database synchronization between these stores. Shipment setup still resolves cargo defaults into the policy snapshots embedded in readings.
* Shipment changes and contract updates can be handled within the same database without synchronizing metadata to a separate vector database.
* Future event-driven processing requires a separately designed ingestion path; this MVP does not assume that a Database Trigger can run directly on the Time Series collection.

## 3. Why Is a Standalone Vector Database Not Enough?

“Put data into a vector database and use RAG” is a common starting point for LLM applications. This project includes RAG, but a vector database alone solves only part of the problem.

### 3.1 Many Questions Require Numerical Computation, Not Semantic Search

“Were there periods above 5°C?”, “How many consecutive minutes?”, and “Which equipment consumed 30% more power than usual?” cannot be answered through embedding similarity. They require exact conditions, time filters, aggregation, and window operations. Vector databases are generally optimized for vector retrieval and simple metadata filtering rather than this combination of operations.

### 3.2 Embedding Sensor Values Is Not a Natural Fit

Turning `{temp: 3.42, powerKw: 2.13}` into a vector does not provide meaningful numerical analysis. Sending raw readings to the LLM instead risks exceeding context limits or incorrect counting. If the fact “24 consecutive minutes outside the permitted range” is wrong, the contract decision built on that fact is also unreliable.

### 3.3 Separate Databases Add Integration Work

Storing time series in a time-series database, cargo specifications in an RDB, and contracts in a vector database introduces the following tradeoffs:

| Concern | Multiple Databases | MongoDB |
|---|---|---|
| Data synchronization | Synchronize shipment and shipper metadata into the vector database | Same database, matching keys |
| Combining facts and evidence | Combine time-series results and vector retrieval results in application code | Two aggregations through the same query interface |
| Query language | SQL, a time-series language, and a vector API | MQL |
| LLM tool design | Separate connections, authentication, and error handling for each system | Shared driver and connection |
| Operations | Separate backup, monitoring, security, and failure handling | One operational platform |

### 3.4 Match the Method to the Data, Keep the Platform Unified

Use aggregation for accurate numerical analysis and vector search for semantic document retrieval. Different data types need different retrieval methods, but storage and querying remain on one platform. This is the project's core architectural principle.

## 4. Usage

### Requirements

* [uv](https://docs.astral.sh/uv/)
* MongoDB 5.0 or later (local Docker replica set or Atlas)
* OpenAI API key (a tool-calling model and an embedding model)

### Installation and Execution

```bash
uv sync
cp .env.example .env          # Set MONGODB_URI and OPENAI_API_KEY

uv run seed.py                # Sample sensor data: 72 hours × 5 reefers = 21,600 readings
uv run ingest_docs.py         # Contracts/SOP/manuals → chunks + embeddings → knowledge
uv run chat.py "Brief me on today's data."
uv run chat.py                # Interactive mode
```

To inspect detection-tool results without an LLM, run `uv run tools.py`.

### Vector Search Environments

`$vectorSearch` requires an environment with the MongoDB Search engine (`mongot`).

| Environment | Retrieval Method |
|---|---|
| Atlas cluster (including the free tier) | `$vectorSearch` |
| Atlas local deployment (Atlas CLI / `mongodb-atlas-local` Docker image) | `$vectorSearch` |
| Regular Community server without Search configured | Automatic application-side cosine similarity fallback |

`ingest_docs.py` attempts to create the index and reports when an unsupported environment requires fallback. The `engine` field in search results (`atlas-vector-search` / `local-cosine-fallback`) identifies the retrieval method. The fallback is intended only for demo-scale document sets.

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `MONGODB_URI` | Connection string | `mongodb://localhost:27017/?directConnection=true` |
| `DB_NAME` | Database name | `coldchain_copilot` |
| `OPENAI_API_KEY` | OpenAI API key | `sk-...` |
| `OPENAI_MODEL` | Chat and tool-calling model | `gpt-5-mini` |
| `OPENAI_EMBED_MODEL` | Document embedding model; rerun `ingest_docs.py` after changing it | `text-embedding-3-small` |

* Local Docker replica set: If member hostnames are registered as container names, the host machine may not resolve them. Use `directConnection=true` to connect directly to the primary port. With authentication enabled, use `mongodb://user:pass@localhost:27017/?directConnection=true&authSource=admin`.
* Atlas: Add your IP address to Network Access.

### LLM Tools

| Tool | Role | Function | Key Mechanism |
|---|---|---|---|
| `get_fleet_overview` | Detection | Summarize temperature, power, and door status by reefer and shipment | `$group`; embedded temperature-policy bounds |
| `find_excursions` | Detection | Compare readings with embedded policy bounds and group consecutive out-of-range readings into incidents per reefer and shipment | `$lt` / `$gt`, `$setWindowFields` + `$shift`; no `$lookup` |
| `find_energy_anomalies` | Detection | Compare recent average power with the preceding 48 hours | `$group` + `$cond` |
| `get_reefer_timeline` | Detection | Retrieve time-bucketed temperature and power trends for a reefer | `$dateTrunc` |
| `search_knowledge` | Evidence | Retrieve relevant contract, SOP, and manual clauses | `$vectorSearch` + metadata filters |
| `run_aggregation` | Ad hoc query | Answer numerical questions not covered by fixed tools (Text-to-MQL) | Read-only; prohibit `$out`/`$merge`; enforce `maxTimeMS` |

### Sample Documents (`docs/`)

| Document | Scope | Clauses Used in the Demo |
|---|---|---|
| BlueHarbor Fisheries Live Fish Transport Contract | SHP-101, SHP-102 / live fish | Notify within 1 hour for excursions lasting at least 15 consecutive minutes; quality inspection; refusal of delivery at 30 cumulative minutes or more |
| OceanPeak Tuna Trading Ultra-Low-Temperature Tuna Transport Contract | SHP-103 / frozen tuna | Notify within 30 minutes when temperature exceeds -50°C; flesh-color inspection at 10 cumulative minutes or more; 10-minute door-opening limit |
| CoastalFresh Seafood Frozen Shrimp Transport Contract | SHP-104 / frozen shrimp | Notify within 24 hours of signs of equipment abnormality and provide a backup equipment plan |
| Cold-Chain Temperature Excursion Response SOP | Shared | Cause classification; refrigeration shutdown and door-opening response; incident report within 24 hours |
| Reefer Preventive Maintenance Manual | Shared | Power increase thresholds of +20%/+40%; inspection of equipment with shutdown history |

All documents are fictional demo materials to be authored as MVP deliverables. Frontmatter, delimited by `---`, defines `docType`, `cargoTypes`, document identity, and version; contract documents also define `contractId`. Ingestion propagates this metadata to chunks. Shipment applicability is managed through `shipments.contractRef`, not shipment-ID arrays in document frontmatter or chunks. The scope column above describes demo assignments, not the chunk schema. To use your own documents, add them to `docs/` in the same format and rerun `ingest_docs.py`.

### Demo Scenarios

`seed.py` injects the following situations relative to its execution time. Sensor facts are designed to match contract conditions.

| Reefer | Cargo (Permitted Range) | Injected Situation | Expected Action Required |
|---|---|---|---|
| RF-02 | Live fish (0–4°C) | Refrigeration stops for 30 minutes about 5 hours ago → approximately 24 minutes of excursion | Notify within 1 hour and arrange quality inspection because the excursion lasts at least 15 minutes. Delivery-refusal condition does not apply because it is under 30 minutes. |
| RF-03 | Frozen tuna (-60 to -50°C) | Door open for 15 minutes during unloading about 3 hours ago → approximately 18 minutes of excursion | Notify within 30 minutes and arrange flesh-color inspection. The 10-minute door limit is violated. |
| RF-04 | Frozen shrimp (-25 to -18°C) | Temperature normal; power consumption increases over the last 24 hours | Notify within 24 hours of signs of equipment abnormality despite no temperature excursion. |
| RF-01, RF-05 | Live fish, fresh oysters | Normal | None |

Scenarios are timestamped relative to the seed execution time. If testing more than a day later, rerun `uv run seed.py`. Rerun `ingest_docs.py` only when documents change.

### Example Question Flow

1. Briefing — Brief me on today's data.
2. Query — Which readings exceeded their permitted ranges?
3. History — Show me RF-02's temperature changes today.
4. Root-cause analysis — Why did this happen to RF-02?
5. Contract assessment — Do we need to notify the shipper for RF-02? Could they refuse delivery?
6. Prevention — Are any reefers using unusually high power despite normal temperatures? Are there contractual actions we need to take?
7. Document question — How long may the door remain open while unloading tuna?

The demo's core flow progresses from simple queries to root-cause analysis, contract assessment, and preventive action.

### Project Structure

```text
coldchain-copilot/
├── design/            Core entity and interface definitions (see Section 5)
├── docs/              RAG documents: contracts, SOP, maintenance manual, with metadata frontmatter
├── seed.py            Create cargo defaults and fleet-scoped shipments; pin policies/contracts; backfill 72 hours of readings with policy snapshots
├── ingest_docs.py     Chunk documents → embed → store in knowledge; create Vector Search index
├── knowledge.py       Document parsing, embedding, retrieval ($vectorSearch or fallback)
├── tools.py           LLM tools: detection aggregations + search_knowledge
├── chat.py            OpenAI tool-calling loop and system prompt (Action Required / Suggestion)
├── pyproject.toml
├── uv.lock
└── .env.example
```
