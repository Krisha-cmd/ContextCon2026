-- Idempotent schema. Safe to apply on every startup.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           text NOT NULL,
    spec                jsonb NOT NULL,
    status              text NOT NULL,
    limits              jsonb NOT NULL,
    input_snapshot_hash text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS steps (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id      uuid NOT NULL REFERENCES jobs(id),
    account_id  uuid,
    step        text NOT NULL,
    status      text NOT NULL,
    output      jsonb,
    lease_owner text,
    lease_until timestamptz,
    attempts    int NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT so job-level steps (account_id NULL) are unique too (PG15+)
    UNIQUE NULLS NOT DISTINCT (job_id, account_id, step)
);
CREATE INDEX IF NOT EXISTS steps_claim_idx ON steps (status, lease_until);

CREATE TABLE IF NOT EXISTS accounts (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id    uuid NOT NULL REFERENCES jobs(id),
    entity_id text,
    name      text,
    domain    text,
    data      jsonb NOT NULL DEFAULT '{}'::jsonb,
    verdicts  jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS accounts_job_idx ON accounts (job_id);

CREATE TABLE IF NOT EXISTS evidence (
    id           text PRIMARY KEY,
    url          text,
    field_path   text,
    quote        text,
    record_date  timestamptz,
    retrieved_at timestamptz NOT NULL,
    raw_hash     text,
    source_name  text NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_keys (
    entity_id   text NOT NULL,
    key_type    text NOT NULL,
    key_value   text NOT NULL,
    evidence_id text REFERENCES evidence(id),
    resolved_by text NOT NULL,
    UNIQUE (key_type, key_value)
);
CREATE INDEX IF NOT EXISTS entity_keys_entity_idx ON entity_keys (entity_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       uuid,
    step         text,
    tbl          text NOT NULL,
    record_id    text NOT NULL,
    before       jsonb,
    after        jsonb,
    reason       text NOT NULL,
    evidence_ids text[] NOT NULL DEFAULT '{}',
    at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_job_idx ON audit_log (job_id, at);

CREATE TABLE IF NOT EXISTS http_cache (
    request_hash text PRIMARY KEY,
    endpoint     text NOT NULL,
    status       int NOT NULL,
    response     jsonb,
    is_negative  boolean NOT NULL DEFAULT false,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS http_cache_expires_idx ON http_cache (expires_at);

CREATE TABLE IF NOT EXISTS entity_cache (
    entity_id  text NOT NULL,
    data_type  text NOT NULL,
    payload    jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (entity_id, data_type)
);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id    uuid REFERENCES jobs(id),
    endpoint  text NOT NULL,
    credits   numeric NOT NULL DEFAULT 0,
    inr       numeric NOT NULL DEFAULT 0,
    ms        int,
    cache_hit boolean NOT NULL DEFAULT false,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cost_ledger_job_idx ON cost_ledger (job_id);

CREATE TABLE IF NOT EXISTS actions (
    idem_key   text PRIMARY KEY,
    job_id     uuid NOT NULL REFERENCES jobs(id),
    account_id uuid,
    type       text NOT NULL,
    payload    jsonb NOT NULL,
    state      text NOT NULL,
    result     jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_key text NOT NULL REFERENCES actions(idem_key),
    decision   text NOT NULL,
    draft_text text,
    final_text text,
    reason     text,
    by_user    text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_action_idx ON approvals (action_key);

CREATE TABLE IF NOT EXISTS parse_rules (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pattern        text NOT NULL,
    mapping        jsonb NOT NULL,
    version        int NOT NULL DEFAULT 1,
    uses           int NOT NULL DEFAULT 0,
    status         text NOT NULL,
    origin_request text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS preferences (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule         text NOT NULL,
    scope        text NOT NULL,
    version      int NOT NULL DEFAULT 1,
    support      int NOT NULL DEFAULT 0,
    source_diffs jsonb,
    status       text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS examples (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draft      text NOT NULL,
    final      text,
    diff       text,
    embedding  vector(768),
    decision   text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS match_labels (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    left_record  jsonb NOT NULL,
    right_record jsonb NOT NULL,
    label        text NOT NULL,
    resolved_by  text NOT NULL,
    at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_stats (
    source       text PRIMARY KEY,
    confirmed    int NOT NULL DEFAULT 0,
    contradicted int NOT NULL DEFAULT 0
);

-- app/runner additions (ALTER ... IF NOT EXISTS keeps the file idempotent).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stop_optional text;  -- 'budget_exceeded' | 'time_limit'
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE steps ADD COLUMN IF NOT EXISTS priority int NOT NULL DEFAULT 0;  -- = step order
ALTER TABLE steps ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz;
ALTER TABLE steps ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';  -- active|dropped|held
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status_reason text;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS status_step text;
CREATE INDEX IF NOT EXISTS steps_job_idx ON steps (job_id, status, priority);

-- Token buckets for app/gateway rate limiting, shared across processes.
CREATE TABLE IF NOT EXISTS rate_buckets (
    bucket     text PRIMARY KEY,
    tokens     double precision NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    step_id  bigint REFERENCES steps(id),
    error    text NOT NULL,
    attempts int NOT NULL,
    at       timestamptz NOT NULL DEFAULT now()
);

-- app/actions outbox (exactly-once side effects). Idempotent additions.
-- (Restored: these lines were lost in the t11 merge.)
ALTER TABLE actions ADD COLUMN IF NOT EXISTS attempts   int NOT NULL DEFAULT 0;
ALTER TABLE actions ADD COLUMN IF NOT EXISTS claimed_by text;
ALTER TABLE actions ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE actions ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS actions_ready_idx ON actions (state, updated_at);
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS payload_hash text;  -- what exactly was approved

-- app/outreach: examples remember which action, approver and segment they came from; the
-- names in `meta` let reflect() reject rules that mention a prospect. One example per action.
ALTER TABLE examples ADD COLUMN IF NOT EXISTS action_key text;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS job_id uuid;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS approver text;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS segment text;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS embed_text text;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS reason text;
ALTER TABLE examples ADD COLUMN IF NOT EXISTS meta jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS examples_action_idx ON examples (action_key);
-- preferences: one row per version. rule_key groups the versions of one rule (its topic);
-- status is active | superseded | disabled | rolled_back.
ALTER TABLE preferences ADD COLUMN IF NOT EXISTS rule_key text;
ALTER TABLE preferences ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE preferences ADD COLUMN IF NOT EXISTS changed_by text;
CREATE INDEX IF NOT EXISTS preferences_key_idx ON preferences (rule_key, scope, version);

-- app/outreach drafts: what the draft step knew when it wrote an email action (fact sheet,
-- reasons, rules, examples used). actions.payload stays exactly what the outbox will run.
CREATE TABLE IF NOT EXISTS drafts (
    action_key text PRIMARY KEY REFERENCES actions(idem_key),
    job_id     uuid NOT NULL REFERENCES jobs(id),
    account_id uuid,
    draft_text text NOT NULL,
    context    jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
