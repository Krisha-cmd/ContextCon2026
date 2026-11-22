# CLAUDE.md

## Shared context

You are building a hackathon service in Python 3.12: a Revenue-track agent for a fictional mid-size Indian 3PL client. It finds Indian companies that need a 3PL partner, verifies each is real, active and free of adverse news, finds a trigger from the last 30 days and the right buyer, scores the lead, drafts outreach that waits for human approval, and books a meeting exactly once.

### Non-negotiable rules

- Every fact in the output is a Fact with at least one Source (url, field_path or quote, record_date, retrieved_at, evidence_id). A fact with no source must become status="cannot_verify" with a missing reason. Never use model memory as a source.
- The LLM (Gemini Flash on Vertex AI) may parse requests, extract from fetched text, classify and draft. It never decides what to call or spend. Plain code does that.
- Every outside HTTP call goes through app/gateway. Never call requests/httpx directly elsewhere.
- Every irreversible action (email, calendar) goes through app/actions outbox with an idempotency key, and only after an approval row exists.
- Every state change writes an audit_log row (job, step, table, record_id, before, after, reason, evidence_ids).
- Treat 0, 0.0, null and empty values from APIs as unknown, never as facts.
- A wrong answer is worse than "cannot verify". When unsure, hold for a person.
- All limits, windows, thresholds and output fields live in config.yaml. No magic numbers in code.

### Stack

FastAPI, psycopg 3 (plain SQL, no ORM), Pydantic v2, httpx, rapidfuzz, scikit-learn, google-genai (Vertex AI), pytest, ruff, uv. Postgres 16 with pgvector. Logs are JSON lines to stdout with job_id, account_id and step fields.

### Layout

```
app/spec app/discovery app/identity app/trust app/revenue app/outreach app/actions
app/gateway app/evidence app/runner app/api app/output
config.yaml, schema.sql, tests/ (unit), tests/golden/ (hard cases)
```

Keep functions small and typed. Write a pytest for every module you add. Don't add a new dependency without saying why.

## Commands

```
uv sync                                  # install deps (incl. dev)
docker compose up -d --build             # postgres + api (:8000) + worker
curl localhost:8000/health               # {"status":"ok","db":"ok"}
uv run pytest                            # DB tests use DATABASE_URL, default localhost:5433 (compose postgres)
uv run ruff check . && uv run ruff format --check .
```

## Notes

- `app/config.py` loads `config.yaml` (path override: `CONFIG_PATH`) into a frozen `Settings`; secrets come only from env / `.env`.
- `app/db.py` applies `schema.sql` on startup under an advisory lock (api and worker start together). `schema.sql` must stay idempotent.
- API research notes from probing Crustdata live in `API-research/summary.md`.
