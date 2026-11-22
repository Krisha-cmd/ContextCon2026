# ContextCon2026

## Web UI

`docker compose up -d --build`, then open http://localhost:8000/?token=<API_TOKEN>.
FastAPI renders the pages with Jinja2. HTMX is vendored in `app/api/static`, so there is no
JavaScript build step. The screens follow `docs/wireframes/wireframe.html`:

| # | Screen | Route |
|---|---|---|
| 1 | New request: text or kickoff JSON, limits and a cost estimate before submit | `GET /`, `POST /jobs` |
| 2 | Live progress, polled every 2 s | `GET /jobs/{id}/progress` |
| 3 | Account table: decision, score, top reasons, trust | `GET /jobs/{id}` |
| 4 | Evidence drawer: every Fact with its sources, plus the audit log | `GET /jobs/{id}/accounts/{aid}` |
| 5 | Approval queue: approve, edit and approve, or reject with a reason | `GET /approvals`, `POST /approvals/{key}` |
| 6 | Outcome: emails, meetings and the client output | `GET /jobs/{id}/outcome`, `GET /jobs/{id}/output` |
| 7 | Run log: audit log and cost ledger | `GET /jobs/{id}/log` |
| | Learned rules: enable, disable or roll back | `GET /preferences` |

The data routes return JSON when the client sends `Accept: application/json` or adds `?format=json`.

### Authentication (demo only)

There are no user accounts. Every route except `/health` needs one shared token, set in the
environment as `API_TOKEN`. Send it in one of these ways:
- an `X-API-Token` header;
- `Authorization: Bearer <token>`;
- `?token=<token>` once in the browser, which sets a cookie.

If `API_TOKEN` is not set, the UI is open and the API logs a warning at startup. This is
enough for a hackathon demo, not for production. Anyone with the token can approve emails, and
the approver's name is whatever the form sends.

## Ethics

**Contact data.** The buyer step stores only a person's name, job title, company, public
profile URL and the Source it came from. It never stores a personal email address or phone
number, even when a provider offers one. A business email or phone may be added only when the
source states it is published for business contact. Raw profiles are not kept: the evidence
row holds a hash of the record, so the claim can be checked against the provider later.

**Adverse news.** A possible issue (fraud, insolvency, regulatory action) never drops an
account automatically. The account is held and a person decides, because naming a company in
connection with wrongdoing is an ethics-sensitive call. When nothing is found, the output says
"no adverse news found" and cites the searches performed; it never calls a company "clean".

## Run it

### Local (docker compose)

```bash
docker compose up -d --build   # postgres (:5433) + api (:8000) + worker   (or: make up)
curl localhost:8000/health     # {"status":"ok","db":"ok"}
open 'http://localhost:8000/?token=local-dev-token'   # web UI (sets a cookie); API: header x-api-token
```

`.env` holds `CRUSTDATA_API_KEY`, `GSTIN_API_KEY`, `GCP_PROJECT` (and optionally
`REGISTRY_API_KEY`). It is never committed or uploaded. The local worker polls for new jobs.
Emails and calendar invites go to a sink file (`var/outbox_sink.jsonl`) unless
`actions.email_mode` / `actions.calendar_mode` in `config.yaml` say otherwise.

### Cloud (GCP)

```bash
gcloud auth login && gcloud config set project <project-id>
make deploy                  # scripts/deploy.sh: asks before creating billable resources
```

This creates (or updates):
- Cloud SQL Postgres 16 (`db-f1-micro`) with pgvector. The worker applies `schema.sql` on start.
- Secret Manager entries `DB_PASSWORD`, `API_TOKEN` (both generated), plus `CRUSTDATA_API_KEY`
  and `GSTIN_API_KEY` from `.env`.
- Cloud Run service `api`, built from source.
- Cloud Run job `worker`, from the same image, running `python -m app.runner.worker --until-idle`.
  The API starts one execution per job with `RUN_JOB_ID` set (`START_MODE=cloud_run`).
- A service account with the roles `cloudsql.client`, `secretmanager.secretAccessor`,
  `aiplatform.user`, `run.invoker` and `logging.logWriter`, plus `run.jobsExecutorWithOverrides`
  on the worker job.

Logs are JSON lines, so Cloud Logging shows them as `jsonPayload` with `severity`. The saved
query "revagent: one job" filters on `jsonPayload.job_id`. Get the API token with
`gcloud secrets versions access latest --secret=API_TOKEN`. `make teardown` deletes the
Cloud SQL instance, service and job.

### Kill test

```bash
make kill-test
```

This hard-kills a worker in the middle of a step, restarts it, and checks that the step
resumes and nothing runs twice. It also crashes the outbox after an email or calendar
side effect but before mark-done, and checks that the restart sees the effect (409 or
already sent) and doesn't send again. It needs the compose Postgres.
