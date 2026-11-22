ContextCon 05 · Revenue track · working blueprint

# 3PL Revenue Agent Blueprint

The service finds Indian companies that need a 3PL partner, proves each one is real and safe to approach, finds a fresh reason and the right person, drafts outreach for human approval, and books the meeting exactly once. Every fact carries a source, or the answer is "cannot verify".

Modular monolith on Cloud Run One Postgres (state, ledger, evidence, vectors) Hybrid funnel + trigger search LLM parses, extracts, drafts. Code decides and spends. Target ≈ ₹1,800–2,200 per job

Part 1

## The research flow

Cheap checks come first, and expensive ones run only on accounts that survive. Every gate has three exits: continue, drop with a reason, or hold for a person when the sources can't settle it.

```mermaid
flowchart TD
  A["Client request"] --> B["Parse: rule engine first, Gemini Flash for the rest"]
  B --> C1["Door 1: Crustdata company search"]
  B --> C2["Door 2: jobs and news from the last 30 days"]
  C1 --> D["Merge and dedup: Crustdata ID, domain, PAN, name+city"]
  C2 --> D
  D -->|likely duplicate| X["Held for a person"]
  D --> E["Free pre-rank, then pick accounts by value per credit"]
  E --> F["Match to legal entity (crosswalk, website footer, scored matcher)"]
  F -->|unsure| X
  F -->|no match| Z["Dropped, reason logged"]
  F -->|match| G["Registry check: GST and MCA status"]
  G -->|inactive or struck off| Z
  G -->|registry unreachable| X
  G -->|active| H["Adverse news check on exact legal name"]
  H -->|issue found, with source| X
  H -->|no adverse news found| I["Batch enrich top 5 to 8 accounts"]
  I --> J{"Dated trigger in last 30 days?"}
  J -->|no| Z
  J -->|yes| K["Find buyer by title, flag stale profiles"]
  K -->|none found| X
  K --> L["Lead score with printed reasons"]
  L --> M["Draft email using learned preference rules"]
  M --> N{"Human approval"}
  N -->|approve or edit| O["Send and book once via action outbox"]
  N -->|reject| Q["Store feedback"]
  O --> Q
  Q --> R["Reflection: update preference rules"]
  R -.-> M
  X -.->|person resolves| S["Stored as label: crosswalk + matcher recalibration"]
  S -.-> F
```

continueheld for a person (cannot verify)dropped with reason

### Stage by stage

| Stage | What happens | Source | Credits (est.) | When sources fail |
| --- | --- | --- | --- | --- |
| 0 · Parse | Request becomes a typed spec: industry, size, region, trigger types, buyer titles, cost and time limits. Active parse rules run first, and Gemini handles only uncovered phrases and proposes new rules. | Rule engine + Gemini Flash, schema-locked JSON | 0 (≤ ₹0.2) | Spec fails validation: ask the requester. |
| 1 · Find | Door 1 searches companies by filters. Door 2 searches job posts and news dated in the window. | Crustdata search, job search, web search | 10–20 | Empty matches are a valid result, not an error. |
| 2 · Dedup + pick | Merge records on strong keys, then rank on fields we already have and spend by expected value per credit. | Local | 0 | held possible duplicates. |
| 3 · Identity | Get the legal name from the website footer or privacy page, then match it to the registry record. | Web fetch, GST/MCA provider | 5–10 | cannot verify when the score lands in the middle band. |
| 4 · Registry | GSTIN active, legal name, state. MCA status: active or struck off. | GST/MCA provider | ₹ per call | cannot verify: registry unavailable |
| 5 · Adverse news | Quoted legal name plus risk words. Fetch each page, confirm the name is in the text, read `datePublished`. | Crustdata news, web search and fetch | 10–20 | Report "no adverse news found", never "clean". |
| 6 · Enrich | Shortlist only, 5 per call: news, hiring, headcount, decision-makers. | Crustdata enrich | 15–25 | 0, null and empty all count as unknown. |
| 7 · Trigger | At least one dated signal inside 30 days, classified by type. | Enrich news and jobs | 0 | dropped when no fresh trigger exists. |
| 8 · Buyer | Person search by title, then the enrich decision-maker list filtered by title. | Crustdata people | 5–15 | cannot verify buyer |
| 9 · Score + draft | Points score with reasons. Draft uses preference rules plus similar approved emails. | Local + Gemini | 0 (≈ ₹2–5) | Draft always waits for approval. |
| 10 · Act | After approval: send and create the calendar event with a deterministic ID. | Calendar and email in test mode | 0 | Outbox retries safely after a kill. |

Credit ranges are rough estimates from the probe (1 credit = $0.30 ≈ ₹25). Measure them with the ledger on the golden set before quoting a price.

Part 2

## Deployment architecture

One codebase builds one container image. The image runs as a web service (request intake, approval page, evidence viewer) and as a job (the pipeline). Postgres is the single source of truth, so either process can die at any time and the next start picks up from the last checkpoint.

```mermaid
flowchart LR
  subgraph People
    U1["Requester"]
    U2["Approver"]
  end
  subgraph GCP["GCP project"]
    API["Cloud Run service: api (FastAPI, approval page)"]
    subgraph WK["Cloud Run Job: worker"]
      SCH["Scheduler: leases, SKIP LOCKED, fair share, aging"]
      RUN["Step runner with checkpoints and audit log"]
      GW["Outbound gateway: rate limit, 4-layer cache, retries, budget guard, cost ledger"]
      OB["Action outbox with idempotency keys"]
    end
    DB[("Cloud SQL Postgres + pgvector")]
    VX["Vertex AI: Gemini Flash, embeddings"]
    SM["Secret Manager"]
    LG["Cloud Logging (JSON lines)"]
  end
  subgraph Outside["External sources and actions"]
    CD["Crustdata API"]
    RG["GST / MCA provider"]
    WB["Web pages (news, company sites)"]
    CAL["Calendar and email, test mode"]
  end
  U1 --> API
  U2 -->|approve or edit| API
  API -->|create job, record approval| DB
  API -->|start execution| RUN
  SCH -->|claim step with lease| DB
  SCH --> RUN
  RUN -->|read and write state| DB
  RUN --> GW
  GW --> CD
  GW --> RG
  GW --> WB
  GW --> VX
  GW -->|cache rows and ledger rows| DB
  RUN --> OB
  OB -->|only after approval| CAL
  OB -->|intent and done markers| DB
  SM -.-> API
  SM -.-> RUN
  RUN -.-> LG
  API -.-> LG
```

### Why this survives the kill on stage

- Each step writes its result and a `done` marker in one transaction. On restart the runner skips finished steps.
- Every outside call goes through the gateway. Its request-hash cache means a replay costs ₹0 and returns the same evidence.
- Actions are written as intent first, with key = hash(job, account, action). The send runs only if no `done` marker exists, and the calendar event ID is that same key, so a second create is rejected.

### Where the brief's checkpoints sit

- **Data calls, retries, cost ledger:** the outbound gateway.
- **Approvals:** the API writes them, and the outbox reads them before acting.
- **Logs:** JSON to stdout, tagged with job, account and step, collected by Cloud Logging.
- **Hour-3 change and live fix:** `config.yaml` (limits, windows, thresholds, output fields) plus Pydantic output models.

### Package layout

```
app/
  spec/          request → run spec (LLM)
  discovery/     door 1 + door 2, dedup
  identity/      legal-entity matching
  trust/         registry + adverse news
  revenue/       trigger, buyer, score
  outreach/      draft, preference learning
  actions/       outbox, calendar, email
  gateway/       rate limit, cache, ledger
  evidence/      Fact model, citations
  runner/        steps, checkpoints
  api/           FastAPI + approval page
config.yaml
tests/golden/    hard cases from the probe
```

### Postgres tables

```
jobs          id, client_id, spec, status, limits,
              input_snapshot_hash
steps         job, account, step, status, output,
              lease_owner, lease_until, attempts
accounts      merged entity, verdicts
entity_keys   entity, key_type, key_value,
              evidence_id, resolved_by
audit_log     job, step, table, record_id,
              before, after, reason, evidence_ids
evidence      url, field_or_quote, record_date,
              retrieved_at, raw_hash
http_cache    request_hash, response, status,
              is_negative, fetched_at, expires_at
entity_cache  entity, data_type, payload, expires_at
cost_ledger   job, endpoint, credits, inr, ms,
              cache_hit
actions       idem_key, type, payload, state
approvals     action, decision, final_text, by
parse_rules   pattern, mapping, version, uses,
              status, origin_request
preferences   rule, scope, version, support,
              source_diffs, status
examples      draft, final, diff, embedding
match_labels  pair, label, resolved_by
source_stats  source, confirmed, contradicted
dead_letter   step, error, attempts
```

**Run it the same way everywhere.** Docker Compose (api, worker, postgres) on a laptop and Cloud Run in the cloud use the same image and config. For the kill on stage, stopping the worker locally is the most predictable. On Cloud Run the equivalent is cancelling the job execution and starting a new one. Deploy with `gcloud run deploy --source .`, which redeploys in about a minute for the live fix.

Part 3

## Integrity, reuse and scheduling

These pieces answer three questions: what changed, why we never pay twice, and how work stays consistent and fair when there is a lot of it.

### Audit log: "which records did you update?"

- An append-only `audit_log` row for every change: job, step, table, record, before, after, reason and the evidence IDs behind it.
- Each job stores a hash of the client data it read, so a mid-build change to a carrier record is visible.
- "What did run 42 change, and why?" is one query, and the approval page shows it per account.

### Four cache layers: never pay twice

- **Request cache:** keyed by a hash of the normalized request, with a TTL per data type (registry 1 day, news 1 day, company basics 7 days).
- **Entity cache:** once an entity is enriched, any query that resolves to it reuses the data.
- **Negative cache:** "not found" is stored with a short TTL, so we don't keep paying to find nothing.
- **Single-flight:** a Postgres advisory lock per request hash, so two workers never fetch the same thing at once.
- Cache hits enter the ledger at ₹0, so the savings show up in the cost records.

### Entity crosswalk: building the missing shared key

- Bridge: domain → website footer → legal name and CIN → GST search on legal name plus state.
- GSTIN characters 3–12 are the PAN, so GSTINs in different states with the same PAN group into one entity.
- Every resolved join, by code or by a person, goes into `entity_keys` with its evidence. The next run looks it up first, for free.

### Parse rule engine: parse once, compile into rules

- Each Gemini parse also proposes reusable mappings, for example "mid-size" → headcount 200–2,000, or "West India" → MH, GJ, GOA, RJ.
- A proposed rule becomes active after a person approves it, or after it gives the same result 3 times.
- New requests run the active rules first. Gemini sees only the uncovered phrases, so LLM calls and variance fall over time.
- Rules are versioned and tested against the golden requests before they go live, to catch over-generalizing.

### Consistency at scale

- Workers claim steps with `SELECT … FOR UPDATE SKIP LOCKED`, so no step runs twice at once.
- Each claim is a lease with a heartbeat. If a worker dies, its lease expires and another worker picks the step up from its checkpoint.
- One transaction per step, plus the action outbox, gives exactly-once effects.
- After N failed attempts a step moves to `dead_letter` instead of retrying forever.

### No starvation

- Deficit round-robin across clients and jobs, so a 500-account job can't block a 5-account one.
- Aging: a waiting step's priority rises with time, so every step eventually runs.
- A per-client concurrency cap, and a central token bucket that gives each active job a fair share of Crustdata's 5 requests a minute.
- At 100x: Pub/Sub with ordering keys, and Cloud Tasks for rate-limited dispatch.

### How the system evolves: four loops, all human-reviewed and reversible

| Loop | Signal | What changes | Guardrail | Proof |
| --- | --- | --- | --- | --- |
| Email preferences | Every approve, edit or reject | Examples update immediately. Rules are distilled in batches. | A rule needs at least 2 supporting edits and holds no prospect personal data | Draft-to-final edit distance falls |
| Matcher | A person resolves a held match | Crosswalk entry plus a new labelled pair. Thresholds are recalibrated. | Precision on the golden set must not drop | The held share falls while precision holds |
| Parse rules | Each Gemini parse | New mappings proposed, then activated | Approval or 3 consistent uses, plus golden tests | LLM calls per request fall |
| Source reliability | A source confirmed or contradicted by another | Per-source trust weight | The weight only reorders sources, it never invents a fact | Fewer conflicts surface late |

Part 4

## ML and algorithms: where, which, why

ML goes where judgement under fuzzy data is the job. It stays out of anything that must be exact: approvals, exactly-once actions, citations and the budget guard are plain code. Each piece has a non-ML version that ships first, so the model is an upgrade, never a dependency.

### Legal-entity matching stage 3 · highest value

Algorithm

Fellegi-Sunter record linkage, or logistic regression over similarity features: Jaro-Winkler and token-set ratio on suffix-normalized names, state match, domain match, PAN equality (GSTIN characters 3–12), director overlap. Calibrate with isotonic regression, then cut into three bands.

Why

Lookalike and namesake test cases are judged here, and a wrong match costs more than "cannot verify". Calibrated bands let you trade coverage for precision on purpose.

Training data

Hand-label 50–100 pairs from the probe (Beardsell twins, Mamaearth and Honasa), plus synthetic lookalikes made by perturbing real names. After that it learns actively: every held match a person resolves becomes a new labelled pair.

Non-ML version

Rule ladder: exact domain or CIN, then exact normalized name plus state, else cannot verify.

Measure

Precision in the match band (aim for 100%) and the share of accounts left as cannot verify.

### Dedup clustering stage 2

Algorithm

Blocking (same domain, first name token, or state) to limit comparisons, the matcher above to score pairs, then union-find to form clusters.

Why

Crustdata returns billed twins, and the test set includes a duplicate. Union-find is linear-time and easy to explain.

Non-ML version

Exact keys only (Crustdata ID, domain, PAN). Anything near-duplicate is held for a person.

### Spending the budget stage 2 · finance story

Algorithm

Greedy knapsack: estimate each account's expected value from cheap search fields, divide by the credits its next steps will cost, and enrich in that order until the stage budget runs out.

Why

Gives a measured, defensible cost per job, and degrades gracefully if the hour-3 change cuts the limit.

Non-ML version

Top N by a fixed fit score.

### Adverse news relevance stage 5

Algorithm

Start with a risk lexicon (fraud, NCLT, insolvency, raid, wilful defaulter) plus a check that the legal name appears near the keyword. Upgrade to Gemini classification that must quote the supporting passage, or a zero-shot NLI model.

Why

Stops a generic scam story or a namesake's news being pinned on the wrong firm, which is an ethics risk as much as a correctness one.

Measure

Precision and recall on about 30 labelled articles.

### Trigger detection stage 7

Algorithm

Regex on job titles ("warehouse", "supply chain", "logistics head") plus LLM labelling of news into a fixed taxonomy (expansion, new warehouse, funding, leadership hire, new market). Weight by recency with exponential decay, e.g. exp(−age/10 days).

Why

The track is literally "meetings on triggers from the last 30 days", and the decay makes fresher triggers rank higher without a hard cliff.

### Buyer identification stage 8

Algorithm

Title normalization, then cosine similarity of Vertex embeddings against target personas ("Head of Supply Chain", "VP Logistics"), times a seniority score from rules, times a freshness penalty for old profiles.

Why

Indian title variety ("GM – SCM", "AVP Distribution") breaks exact matching, and embeddings catch these cheaply.

Non-ML version

A title dictionary with seniority ranks.

### Lead scoring stage 9

Now

An additive points model (trigger strength × recency, fit, buyer confidence, trust status), with each contribution printed beside the score.

Later

Logistic regression or LightGBM with monotonic constraints once there are outcome labels (reply, meeting held), explained with per-feature contributions.

Why

The ethics judge scores how the service explains its decisions. A transparent score answers that directly.

### Self-improving outreach stage 9–10 · learning loop

Algorithm

Two kinds of memory. Episodic: every approve, edit or reject is stored at once (draft, final, diff, reason, embedding), and k-nearest-neighbour retrieval in pgvector feeds the three most similar approved emails into the next draft, so it adapts with every use. Semantic: a reflection step (one LLM call per batch) turns repeated edits into explicit, versioned rules scoped to the company, an approver or a segment. A rule needs at least 2 supporting edits, and when two rules conflict the newer one wins.

Later

A contextual bandit (Thompson sampling) over templates or subject lines, rewarded by reply rate.

Why

Learning lives in readable rules, not hidden weights. The approver can see, edit or roll back any rule.

Measure

Edit distance from draft to approved text across runs. A falling line is the proof.

### Request parsing stage 0

Algorithm

Gemini Flash with a response schema mapped to the Pydantic run spec, with validation and one repair retry.

Why

One call per job lets the unseen finals request work without new code. If the kickoff input format is structured JSON, map its fields directly and use the LLM only for free-text parts.

Part 5

## Ready answers for the design question

### 100 times the volume

- Split the worker by stage behind Pub/Sub. The packages are already service boundaries.
- Move from Cloud Run to GKE with ArgoCD when the service count justifies it.
- Share the evidence cache across clients, since company facts repeat across jobs. Cost per job falls as volume grows.
- Spread Crustdata's rate limit with a central token bucket, and move analytics on the ledger to BigQuery.

### A second client

- Add `client_id` to every table, with row-level security in Postgres.
- Give each client its own config, preference rules, approvers and budget.
- Share the public-fact cache, but never preferences or drafts.