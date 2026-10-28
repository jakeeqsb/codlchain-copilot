# ColdChain Copilot

An operations agent for cold-chain logistics. It reads reefer sensor data, checks it against shipper contracts and SOPs (standard operating procedures), and tells operators **what happened and what to do next**.

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

Every answer this agent gives combines four things: sensor facts, shipment context, contract text, and rules. MongoDB keeps all four in one database, so each answer is one query path instead of an integration between systems.

```js
// shipments — who, what, which policy, which contract
{
  shipmentId: "SHP-102",
  fleetId: "FLEET-01",
  reeferId: "RF-02",
  cargoType: "cargo:live_fish",
  temperaturePolicy: {
    minC: 0,
    maxC: 4,
    policyVersion: "SHP-102-policy-v1"
  },
  contractRef: {
    contractId: "blueharbor-live-fish",
    version: "v1"
  }
}

// readings (Time Series) — measurements + stable context in meta
{
  ts: ISODate("2026-10-15T00:12:00Z"),
  meta: {
    fleetId: "FLEET-01",
    reeferId: "RF-02",
    shipmentId: "SHP-102",
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

// clauses — a contract article as a rule, linked to its source text
{
  clauseId: "blueharbor-live-fish-v1-art6",
  contractId: "blueharbor-live-fish",
  version: "v1",
  trigger: {
    eventType: "event:temperature_excursion",
    allOf: [
      { measure: "consecutiveMinutes", op: "gte", value: 15 }
    ]
  },
  action: {
    type: "notify",
    party: "shipper.qa"
  },
  deadline: {
    anchor: "eventStart",
    offsetMinutes: 60
  },
  chunkId: "blueharbor-live-fish-v1-article-6"
}
```

**Documents match the data.** A shipment is read as one object with its route, policy, and contract reference. Each reading carries its shipment's temperature policy (Extended Reference pattern), so detection needs no join and past readings keep the threshold they were recorded under. Clauses of different shapes share one collection (polymorphic documents), and new sensors or fields need no migration (flexible schema).

**Time Series is built in.** Minute-level readings are stored in compressed buckets for efficient writes and storage, and queried with the same aggregation framework that turns consecutive out-of-range readings into excursions.

**Vector Search sits beside the data.** Contract chunks are searched by meaning and filtered by the contract version stored on the shipment, so there is no metadata to copy into a separate vector store and keep in sync.

**Graph relations stay inside, where they are shallow.** Clause dependencies are one or two hops, followed with `$graphLookup`. Network-scale analysis would call for a graph database alongside MongoDB, not instead of it.

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

Detailed rationale and tradeoffs: [`design/decisions.md`](./design/decisions.md) and [`design/entities.md`](./design/entities.md).

---

## 3. From Retrieval to Decisions: Why an Ontology

In the five question levels of [§ 1](#1-what-it-does), RAG covers level 3, document search: *"what does the contract say?"* A briefing also needs level 5, rule decisions: *"what must we do, and by when?"* That is a judgment, and in this domain a wrong one has a cost: a missed notification deadline means paying for the freight and the lost cargo. An ontology is what turns retrieved text into decisions the system can make consistently.

### What happens with RAG alone

RAG finds contract passages that look similar to the question, shows them to the LLM, and lets the LLM read them and answer. It is like pulling a few pages that seem relevant out of a contract and handing them to a new hire to decide. Ask about RF-02 (above 4°C for 24 minutes in a row), and three things can go wrong.

**1. It misses a clause it should have found.**
Ask "do we need to notify the shipper?" and it finds Article 6, which contains the word "notify." But Article 7 only says "if Article 6 applies, inspect the cargo before unloading," with nothing like "notify" in it. It can be left out of the search, and the cargo gets unloaded without inspection.

**2. The same question gets different answers.**
Article 6 requires "15 consecutive minutes or more"; Article 8 requires "30 cumulative minutes or more." Because the LLM decides by reading the sentences which one 24 minutes falls under, the answer can change from one run to the next. Sometimes it may wrongly say "delivery could be refused."

**3. It says "no problem" when it simply does not know.**
The oyster contract requires humidity checks, but the reefer has no humidity sensor. With no data to check, the LLM tends to summarize it as "nothing unusual." In reality nothing was checked, yet it looks like a clean result.

### With the ontology

An ontology is close to a rulebook prepared from the contract in advance. For each clause it stores, as data, when it applies, what must be done, and which clauses it is connected to.

1. Article 7 is stored as "connected to Article 6," so when Article 6 applies, Article 7 comes with it.
2. Conditions are stored as numbers, so the system just compares values. 24 minutes is at least 15, so Article 6 applies; it is under 30, so Article 8 does not. The answer is the same no matter how many times you ask.
3. When the data a decision needs is missing, it is marked separately as "needs confirmation."

The LLM still writes the briefing, and RAG is still used: it finds the source text of each decided clause to quote, and it answers questions no rule covers ("what should we watch for when unloading tuna?"). What changes is **who makes the decision**. Aggregation computes the numbers, rules decide the obligations, and the LLM explains the result.

### What MongoDB contributes to an AI ontology

- **Ontology and data in one place.** Classes, relations, and rules are documents next to `readings` and `shipments`. Evaluating a rule is an aggregation over the same data, with no export to a separate reasoner.
- **Semantic and structural retrieval in one pipeline.** `$vectorSearch` finds the clause a question means, `$lookup` attaches its rule, and `$graphLookup` follows its dependencies — a GraphRAG-style flow inside one aggregation.
- **Grounded context for the LLM.** Tools return decisions with their basis (fact, condition, source chunk). The LLM explains a result instead of inferring one, and the defined vocabulary keeps its tool calls and generated queries on known names.
- **Room to grow.** New clause kinds or measures are new fields in polymorphic documents, not schema migrations.

### Scope

The MVP keeps the ontology minimal: two class hierarchies (cargo, events), six relations, and one rule format, with clauses structured by hand. Clauses that do not fit the format stay as text and are cited, not decided. If decisions are low-stakes or a person reviews every answer anyway, RAG alone is enough; the ontology pays off when the same contract terms apply to many shipments every day.

---

## 4. Quick Start

**Requirements:** [uv](https://docs.astral.sh/uv/), a MongoDB deployment with Vector Search, a Voyage AI API key (embeddings), an OpenAI API key (briefing LLM).

```bash
uv sync
cp .env.example .env          # set MONGODB_URI, VOYAGE_API_KEY, OPENAI_API_KEY

uv run seed.py                # shipments, ontology, clauses, 72 h of readings
uv run ingest_docs.py         # docs/ → chunks + embeddings → knowledge
uv run chat.py "Brief me on today's data."
uv run chat.py                # interactive mode
```

| Variable | Example |
|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` |
| `DB_NAME` | `coldchain_copilot` |
| `VOYAGE_API_KEY` | your Voyage AI key |
| `VOYAGE_EMBED_MODEL` | `voyage-4` |
| `OPENAI_API_KEY` | your OpenAI key |
| `OPENAI_MODEL` | `gpt-5-mini` |
| `MAX_WINDOW_HOURS` | `72` |

Provisioning MongoDB and the API keys is up to you; the project only reads these variables.

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
├── data/            Ontology, clauses, shipments, cargo defaults (JSON, loaded by seed.py)
├── design/          Detailed design (see below)
├── seed.py          Shipments, ontology, clauses, and readings with policy snapshots
├── ingest_docs.py   Chunk, embed, and load documents; create the Vector Search index
├── knowledge.py     Document retrieval ($vectorSearch)
├── tools.py         LLM tools: lookup, detection, retrieval, rule evaluation
└── chat.py          Tool-calling loop and system prompt
```

| Design document | Contents |
|---|---|
| [`design/entities.md`](./design/entities.md) | Collections, document schemas, seeded data |
| [`design/ontology.md`](./design/ontology.md) | Classes, relations, measures, rule format, evaluation |
| [`design/interfaces.md`](./design/interfaces.md) | Tools by level, parameters, return shapes, errors, briefing format |
| [`design/decisions.md`](./design/decisions.md) | Key design decisions and when to revisit them |
| [`design/scaling.md`](./design/scaling.md) | Shard-key and index candidates, what to measure before scaling |
