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
