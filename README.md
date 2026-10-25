# ColdChain Copilot

An operations agent for cold-chain logistics. It reads reefer sensor data, checks it against shipper contracts and SOPs, and tells operators **what happened and what to do next**.

*MongoDB Solo Project · Time Series × Vector Search × Ontology × LLM tool calling*

```text
> Brief me on today's data.
  ↳ find_excursions({"fleet_id": "FLEET-01", "hours": 24})
  ↳ find_energy_anomalies({"fleet_id": "FLEET-01", "hours": 24})
  ↳ evaluate_obligations({"shipment_id": "SHP-102"})
  ↳ ...

Action Required
· [RF-02 / SHP-102] Notify BlueHarbor QA by 10:12 — 24 consecutive min above 4°C (≥ 15)
  [BlueHarbor Live Fish Contract v1 § Art. 6]
· [RF-02 / SHP-102] Quality inspection before unloading — follows from Art. 6
  [BlueHarbor Live Fish Contract v1 § Art. 7]

Needs Confirmation
· [RF-05 / SHP-105] Humidity clause cannot be checked — humidity is not collected
  [TidalPearl Oyster Contract v1 § Art. 5]

Suggestion
· [RF-02] Inspect compressor and power system before reassignment  [Maintenance Manual § 4]
```

---

## 1. What It Does

Each reefer (refrigerated container) reports temperature, compressor power, and door status every minute. Detecting a threshold breach is easy. The real work comes after:

- **Why** did it happen — compressor stop or an open door?
- What does **this shipper's contract** require, and by when?
- What do **SOPs and maintenance manuals** recommend?
- Which equipment is **about to fail**, even if nothing has happened yet?

The facts live in sensor data; the rules live in documents. ColdChain Copilot connects the two, and handles questions at five levels on the same data:

| Level | Example question | How it is answered |
|---|---|---|
| 1 · Lookup | *Where is SHP-102 going?* | Shipment document |
| 2 · Sensor analysis | *Which readings exceeded their range today?* | Time Series aggregation |
| 3 · Document search | *How long may the door stay open while unloading tuna?* | Vector Search (RAG) |
| 4 · Facts + evidence | *Brief me on today's data.* | Levels 2 + 3 + 5 combined |
| 5 · Rule decisions | *What must we do for RF-02? Could they refuse delivery?* | Ontology: clauses as rules |

```text
 Question ──▶ LLM agent (chooses tools, repeats as needed)
                 │
   ① Detect     readings    aggregation      → "RF-02: 24 consecutive min above 4°C"
   ② Scope      shipments   find             → SHP-102 → BlueHarbor contract v1
   ③ Decide     clauses     rule evaluation  → Art. 6 triggered (24 ≥ 15)
                            $graphLookup     → Art. 7 depends on Art. 6 → triggered
   ④ Quote      knowledge   $vectorSearch    → source text of each clause
                 │
   ⑤ Explain    LLM writes Action Required / Needs Confirmation / Suggestion
```

### Design Principles

1. **The database calculates; the LLM explains.** Sensor facts come from aggregation. The LLM never counts raw readings.
2. **Rules are data.** Whether a clause applies is decided by comparing named measures with structured conditions, not by the LLM reading a sentence.
3. **Evidence comes from the right contract.** Clauses are used only from the contract version assigned to the shipment.
4. **No evidence, no conclusion.** If the data or clause needed for a decision is missing, the answer is "needs confirmation" — never "no obligation."

---

## 2. Why MongoDB

The agent needs four kinds of data at once: sensor time series, shipment context, documents with embeddings, and rules. MongoDB holds them in one document model, one query language, and one cluster.

```js
// shipments — who, what, which policy, which contract
{ shipmentId: "SHP-102", fleetId: "FLEET-01", reeferId: "RF-02", cargoType: "live_fish",
  temperaturePolicy: { minC: 0, maxC: 4, policyVersion: "SHP-102-policy-v1" },
  contractRef: { contractId: "blueharbor-live-fish", version: "v1" } }

// readings (Time Series) — measurements + stable context in meta
{ ts: ISODate("2026-10-15T00:12:00Z"),
  meta: { fleetId: "FLEET-01", reeferId: "RF-02", shipmentId: "SHP-102", cargoType: "live_fish",
          temperaturePolicy: { minC: 0, maxC: 4, policyVersion: "SHP-102-policy-v1" } },
  temp: 3.42, powerKw: 2.13, doorOpen: false }

// clauses — a contract article as a rule, linked to its source text
{ clauseId: "blueharbor-live-fish-v1-art6", contractId: "blueharbor-live-fish", version: "v1",
  trigger: { eventType: "event:temperature_excursion",
             allOf: [ { measure: "consecutiveMinutes", op: "gte", value: 15 } ] },
  action: { type: "notify", party: "shipper.qa" },
  deadline: { anchor: "eventStart", offsetMinutes: 60 },
  chunkId: "blueharbor-live-fish-v1-article-6" }
```

### 2.1 Document Model: The Data Is Shaped Like Documents

The question is not "can a relational schema store this?" — it can. The question is which model matches the shape of this data and the way the agent reads it.

**Each business object is read whole.** An operator asks about *a shipment* or *an excursion*, not about rows. A shipment, with its route, resolved policy, and contract reference, is one document; the agent gets everything it needs in one read and passes it to the LLM as-is. In a normalized schema the same answer is a join across shipments, routes, policies, and contracts, reassembled in application code before the LLM can see it.

**Point-in-time context belongs with the fact.** A reading is only meaningful together with the threshold that applied *when it was recorded*. Embedding the policy snapshot in each reading makes that relationship permanent and join-free. The relational equivalent is an effective-dated policy table and a temporal join on every detection query — more work, and easier to get wrong when a default changes.

**Rules are not uniform.** Every contract has different articles, and each article has a different number of conditions, its own action, and an optional deadline or dependency. As documents, each clause stores exactly its own structure — a `trigger.allOf` array of any length, nested `action` and `deadline`. In tables this becomes either many nullable columns or a generic attribute–value table that is hard to query and validate.

**The schema evolves with the domain.** Adding a humidity sensor, a new event type, or a contract field is a new field, not a migration across every table that references it. That matters in IoT, where payloads differ by vendor and change over time.

### 2.2 Time Series: Efficient Storage, Ordinary Queries

Readings with the same `meta` are grouped into compressed buckets, so the policy snapshot is stored once per bucket, not once per reading, and indexes point to buckets rather than individual readings. Queries still use the standard aggregation framework: `$setWindowFields` turns consecutive out-of-range readings into events, and `$dateTrunc` builds trends. There is no separate time-series query language and no separate store to synchronize.

### 2.3 Vector Search: Retrieval Beside the Data

Document chunks and embeddings live in the same database as shipments. `$vectorSearch` combines semantic similarity with filters on `contractId` and `version` in one stage, so a similar clause from another shipper's contract is never used as evidence — and the filter values come from the shipment document, not from metadata copied into a separate vector store.

### 2.4 Graphs: What MongoDB Handles Here, and Where a Graph Database Fits

MongoDB is not a graph database and this project does not claim to replace one. The point is narrower: **the graph in this demo is small, shallow, and attached to operational documents**, which is the case where modeling it in MongoDB is the better choice.

**Where MongoDB is the right place for the graph (this demo)**

| Relationship | Shape | How it is handled |
|---|---|---|
| Shipment → contract version | 1 hop | A field: `shipments.contractRef` |
| Cargo class → parent classes | Fixed, rarely changes | Precomputed `ancestors` array, matched with `$in` |
| Clause → clause it depends on | 1–2 hops | `$graphLookup` on `clauses.references` |

These relationships share three properties:
- **Every traversal starts from an operational fact** — this shipment, this excursion — and the result is combined with sensor facts in the same pipeline.
- **Depth is small and bounded.** Nothing here needs more than two hops.
- **Edges are attributes of documents that already exist.** No separate edge store, no synchronization, no second consistency problem.

**Where MongoDB's graph support stops**

`$graphLookup` follows one connection field recursively from a set of starting documents, within a per-stage memory limit. It does not provide:
- Shortest-path or weighted-path queries
- Variable-length pattern matching across several edge types in one query (for example Reefer → Compressor model → Part batch → Supplier → other Reefers)
- Graph algorithms such as centrality, community detection, or propagation scoring
- Ontology reasoning: inferring new facts from class and property definitions (RDF/OWL-style)

As depth, fan-out, and the number of edge types grow, each hop becomes another index lookup inside an aggregation stage. A graph database stores adjacency natively and is built for exactly these queries.

**When to add a graph database, and how to split the work**

If the product grows into questions like *"Which in-transit shipments share a compressor part batch with RF-02, and through which depots and suppliers?"* or *"Which depot is the most common point of failure across incidents this quarter?"*, those are path and network-analysis questions. A sensible split:

| Responsibility | Where | Why |
|---|---|---|
| Sensor history, shipments, documents, embeddings, per-shipment rule evaluation | MongoDB (system of record) | Read and written as documents; high write volume; queried by time and scope |
| Equipment/supplier/depot network, multi-hop impact paths, graph analytics | Graph database | Relationship-centric, variable depth, needs path algorithms |
| Connecting the two | IDs as the shared key; the graph holds a projection of relationships, MongoDB holds the full records | The graph answers "which entities"; MongoDB answers "what do we know about them" |

For the MVP, every question stays in MongoDB's range, so no graph database is needed. The design keeps stable IDs on every entity so a graph projection can be added later without remodeling.

### 2.5 One Platform

Detection, retrieval, and rule evaluation are all pipelines on one driver and one connection. There is no ETL and no metadata sync between systems.

```text
 Typical stack for this workload           MongoDB
 ┌───────────┐ ┌──────────┐ ┌──────────┐    ┌──────────────────────────────┐
 │Time-series│ │   RDB    │ │Vector DB │    │ readings   (Time Series)     │
 │ (sensors) │ │(context, │ │  (text)  │    │ shipments                    │
 │           │ │  rules)  │ │          │    │ knowledge  (Vector Search)   │
 └─────┬─────┘ └────┬─────┘ └────┬─────┘    │ clauses · ontology           │
       └─ ETL · sync · app joins ─┘         └──────────────┬───────────────┘
                    │                                      │ MQL
                   App ── LLM                             App ── LLM
```

---

## 3. RAG and Ontology: Why Both

**RAG finds evidence. The ontology turns evidence into rules the database can evaluate.**

| | RAG (vector search) | Ontology (structured clauses) |
|---|---|---|
| Works on | Sentences | Concepts, relations, conditions |
| Finds by | Similar meaning | Defined relations |
| Decides | The LLM, by reading | The database, by comparing values |
| Strong at | New or open questions | Completeness, same input → same answer |
| Weak at | Missing related clauses, unstable judgment | Anything not modeled in advance |

In this project they work together. The ontology decides which clauses are triggered; RAG retrieves their source text for citation and answers questions the rules do not cover.

### Why a standalone vector database is not enough

- **Most questions are numeric.** "How many consecutive minutes above 4°C?" needs filters, aggregation, and window functions — not embedding similarity.
- **Sensor values do not embed meaningfully.** Pushing raw readings into the LLM's context leads to miscounts. If "24 consecutive minutes" is wrong, every contract decision built on it is wrong too.
- **Splitting data across systems adds integration work** — synced metadata, app-side joins, several query languages, and separate operations for each store.

---

## 4. Quick Start

> The code is being rebuilt stage by stage to match this design. See [build plan](./design/build-plan.md) for what works now.

**Requirements:** [uv](https://docs.astral.sh/uv/), MongoDB 5.0+ (Atlas or local), an OpenAI API key.

```bash
uv sync
cp .env.example .env          # set MONGODB_URI and OPENAI_API_KEY

uv run seed.py                # shipments, ontology, clauses, 72 h of readings
uv run ingest_docs.py         # docs/ → chunks + embeddings → knowledge
uv run chat.py "Brief me on today's data."
uv run chat.py                # interactive mode
```

| Variable | Example |
|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017/?directConnection=true` |
| `DB_NAME` | `coldchain_copilot` |
| `OPENAI_API_KEY` | `sk-...` |
| `OPENAI_MODEL` | `gpt-5-mini` |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` |

- **Local replica set in Docker:** use `directConnection=true` to connect straight to the primary.
- **Vector Search** requires Atlas or the `mongodb-atlas-local` image. On a plain Community server, retrieval falls back to in-app cosine similarity; the `engine` field in search results shows which one ran.

---

## 5. Demo

`seed.py` injects these situations relative to the time it runs. Rerun it if testing more than a day later.

| Reefer | Cargo (range) | Injected situation | Expected outcome |
|---|---|---|---|
| RF-02 | Live fish (0–4°C) | Compressor stops 30 min → ~24 min excursion | Notify within 1 h (Art. 6) → inspection (Art. 7). Refusal at ≥ 30 min (Art. 8) does **not** apply. |
| RF-03 | Frozen tuna (−60 to −50°C) | Door open 15 min during unloading → ~18 min excursion | Notify within 30 min, flesh-color inspection, 10-min door limit violated. |
| RF-04 | Frozen shrimp (−25 to −18°C) | Normal temperature, power rising over 24 h | Report equipment abnormality within 24 h, despite no excursion. |
| RF-05 | Oysters (1–5°C) | Normal temperature; contract also requires humidity | **Needs confirmation**: humidity is not collected. |
| RF-01 | Live fish (0–4°C) | Normal | — |

Try the levels in order:

1. *Where is SHP-102 going?*
2. *Show me RF-02's temperature today. Why did it go out of range?*
3. *How long may the door stay open while unloading tuna?*
4. *Brief me on today's data.*
5. *What must we do for RF-02? Could they refuse delivery?* · *Any contract issue for SHP-105?*

All companies, contracts, SOPs, and manuals are fictional demo material.

---

## 6. Project Structure

```text
coldchain-copilot/
├── docs/            Contracts, SOP, maintenance manual (RAG sources)
├── design/          Detailed design (see below)
├── seed.py          Shipments, ontology, clauses, and readings with policy snapshots
├── ingest_docs.py   Chunk, embed, and load documents; create the Vector Search index
├── knowledge.py     Document retrieval ($vectorSearch, with fallback)
├── tools.py         LLM tools: lookup, detection, retrieval, rule evaluation
└── chat.py          Tool-calling loop and system prompt
```

| Design document | Contents |
|---|---|
| [`design/entities.md`](./design/entities.md) | Collections, document schemas, seeded data |
| [`design/ontology.md`](./design/ontology.md) | Classes, relations, measures, rule format, evaluation |
| [`design/interfaces.md`](./design/interfaces.md) | Tools by level, parameters, return shapes, errors, briefing format |
| [`design/decisions/MVP-001.md`](./design/decisions/MVP-001.md) | Query-time detection on embedded policy snapshots |
| [`design/decisions/MVP-002.md`](./design/decisions/MVP-002.md) | Clauses as rules: a minimal ontology |
| [`design/scaling.md`](./design/scaling.md) | Shard-key and index candidates, what to measure before scaling |
| [`design/build-plan.md`](./design/build-plan.md) | Stages, migration from the prototype, learning checkpoints |
