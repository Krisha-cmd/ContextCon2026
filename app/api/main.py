"""HTTP API and web UI: FastAPI + server-rendered Jinja2 + HTMX (vendored, no JS build step).

Screens (docs/wireframes/wireframe.html): 1 new request (/), 2 live progress
(/jobs/{id}/progress), 3 account table (/jobs/{id}), 4 evidence drawer
(/jobs/{id}/accounts/{aid}), 5 approval queue (/approvals), 6 outcome (/jobs/{id}/outcome),
7 run log (/jobs/{id}/log), plus learned rules (/preferences).
Data routes answer JSON when the client asks for it (Accept: application/json, or
?format=json); browsers get HTML. Auth: one shared token from env API_TOKEN (demo only).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import db
from app.actions import outbox
from app.api import queries as q
from app.api import start, views
from app.config import Settings, get_settings
from app.log import get_logger, setup_logging
from app.output.render import render_job
from app.outreach import learning
from app.outreach.draft import draft_text
from app.runner.pipeline import PIPELINE
from app.runner.registry import REGISTRY
from app.spec.parser import SpecInvalid

log = get_logger(__name__)

HERE = Path(__file__).resolve().parent
OPEN_PATHS = ("/health", "/static/")
TOKEN_HEADER = "x-api-token"
HTTP_SEE_OTHER, HTTP_UNAUTHORIZED = 303, 401


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log.level)
    db.init_db(settings)
    if settings.secrets.api_token is None:
        log.warning("API_TOKEN is not set: the UI and API are open to anyone who can reach them")
    log.info("api started")
    yield
    db.close_pool()


app = FastAPI(title="revagent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _inr(value: Any) -> str:
    return f"₹{float(value or 0):,.2f}"


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    return "date unknown" if not value else str(value)[:16].replace("T", " ")


templates.env.filters.update(
    inr=_inr, when=_when, short=views.short, points=views.points,
    pretty=lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str),
)  # fmt: skip


# ---- auth ---------------------------------------------------------------------------------
def _token_ok(request: Request, token: str) -> bool:
    s = get_settings()
    given = [
        request.headers.get(TOKEN_HEADER),
        request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None,
        request.cookies.get(s.api.token_cookie),
        request.query_params.get("token"),
    ]
    return any(g and hmac.compare_digest(g, token) for g in given)


@app.middleware("http")
async def shared_token(request: Request, call_next: Any) -> Response:
    """Demo auth: one shared token (env API_TOKEN). ?token=... once sets a cookie."""
    secret = get_settings().secrets.api_token
    if secret is None or request.url.path.startswith(OPEN_PATHS):
        return await call_next(request)
    token = secret.get_secret_value()
    if not _token_ok(request, token):
        return Response("unauthorized: pass the shared token", status_code=HTTP_UNAUTHORIZED)
    resp = await call_next(request)
    if request.query_params.get("token") == token:
        resp.set_cookie(get_settings().api.token_cookie, token, httponly=True, samesite="lax")
    return resp


# ---- helpers ------------------------------------------------------------------------------
def wants_json(request: Request) -> bool:
    if request.query_params.get("format") == "json":
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"s": get_settings(), **ctx})


def learning_deps() -> learning.LearningDeps:
    """Tests replace this to fake the LLM and embedder."""
    return learning.LearningDeps()


def _job_or_404(conn: Any, job_id: str) -> dict[str, Any]:
    try:
        job = q.job(conn, job_id)
    except Exception as exc:  # malformed uuid
        raise HTTPException(status_code=404, detail="no such job") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job


def _header(conn: Any, job: dict[str, Any]) -> dict[str, Any]:
    spent = q.ledger(conn, job["id"])
    return {"job": job, "ledger": spent, "limit_inr": (job["limits"] or {}).get("max_inr_per_job")}


@app.exception_handler(SpecInvalid)
async def spec_invalid_handler(_: Request, exc: SpecInvalid) -> JSONResponse:
    """The requester sees why their request could not be parsed, so they can rephrase it."""
    return JSONResponse(
        {"error": "spec_invalid", "message": str(exc), "details": exc.errors}, status_code=422
    )


@app.get("/health")
def health() -> JSONResponse:
    try:
        ok = db.ping()
    except Exception as exc:  # report, don't crash the probe
        log.warning("health db check failed", extra={"error": str(exc)})
        ok = False
    body = {"status": "ok" if ok else "degraded", "db": "ok" if ok else "down"}
    return JSONResponse(body, status_code=200 if ok else 503)


# ---- 1 · new request ----------------------------------------------------------------------
def estimate_inr(s: Settings) -> float:
    """A rough upper estimate of one job's spend from config prices (shown before submit)."""
    e, c, n = s.crustdata.est_credits, s.crustdata, s.shortlist
    credits = (
        c.page_size.company_search * s.discovery.search_max_pages * e.company_search_per_result
        + c.page_size.job_search * e.job_search_per_result
        + s.discovery.news_max_queries * e.web_search_per_page
        + n.pre_rank_top * (e.web_search_per_page + s.news.max_fetch * e.web_fetch_per_url)
        + n.enrich_top * e.company_enrich_per_domain
        + n.enrich_top * c.page_size.person_search * e.person_search_per_result
    )
    registry = n.pre_rank_top * s.gstinapi.est_credits_per_lookup * s.gstinapi.credit_inr
    llm = (2 + n.enrich_top * s.outreach.draft_attempts) * s.vertex.est_inr_per_call
    return round(credits * s.limits.credit_inr + registry + llm, 2)


@app.get("/", response_class=HTMLResponse)
def new_request(request: Request) -> HTMLResponse:
    with db.tx() as conn:
        jobs = q.recent_jobs(conn)
    return page(request, "new.html", jobs=jobs, estimate=estimate_inr(get_settings()))


@app.post("/jobs")
async def create_job(
    request: Request,
    text: Annotated[str | None, Form()] = None,
    kickoff: Annotated[UploadFile | None, File()] = None,
    budget_inr: Annotated[float | None, Form()] = None,
    max_minutes: Annotated[float | None, Form()] = None,
) -> Response:
    """Text or kickoff JSON -> a job, started (steps inserted; Cloud Run Job in the cloud)."""
    s = get_settings()
    kickoff_data: Any = None
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        text, kickoff_data = (body.get("text") or body.get("request")), body.get("kickoff")
        budget_inr, max_minutes = body.get("budget_inr"), body.get("max_minutes")
    elif kickoff is not None and kickoff.filename:
        kickoff_data = await kickoff.read()
    try:
        spec = start.spec_from(text, kickoff_data, s.api.max_upload_kb)
    except start.RequestInvalid as exc:
        if wants_json(request) or request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with db.tx() as conn:
            jobs = q.recent_jobs(conn)
        return page(request, "new.html", jobs=jobs, estimate=estimate_inr(s), error=str(exc),
                    text=text)  # fmt: skip
    job_id = await start.start(s, REGISTRY, spec, start.form_limits(budget_inr, max_minutes))
    if wants_json(request) or request.headers.get("content-type", "").startswith(
        "application/json"
    ):
        return JSONResponse({"job_id": str(job_id)}, status_code=201)
    return RedirectResponse(f"/jobs/{job_id}/progress", status_code=HTTP_SEE_OTHER)


# ---- 2 · live progress --------------------------------------------------------------------
@app.get("/jobs/{job_id}/progress", response_class=HTMLResponse)
def job_progress(request: Request, job_id: str) -> HTMLResponse:
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        ctx = _header(conn, job)
        steps = views.progress(PIPELINE, q.step_rows(conn, job["id"]))
        counts = views.summary_counts(q.accounts(conn, job["id"]))
    name = "_progress.html" if is_htmx(request) else "progress.html"
    return page(request, name, steps=steps, counts=counts, **ctx)


# ---- 3 · account table --------------------------------------------------------------------
def _account_rows(rows: list[dict[str, Any]], s: Settings) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        data = r["data"] or {}
        out.append({
            "row": r, "decision": views.decision(r), "score": data.get("score"),
            "reasons": views.top_reasons(data, s.api.top_reasons), "trust": views.trust(data),
            "trigger": data.get("trigger"), "buyer": data.get("buyer"),
        })  # fmt: skip
    order = {"proceed": 0, "held": 1, "dropped": 2}
    return sorted(out, key=lambda a: (order.get(a["decision"]["decision"], 3),
                                      -(a["score"] or -1e9), a["row"]["name"] or ""))  # fmt: skip


@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str) -> Response:
    """Status, the per-account table (decision, score, reasons) and the cost ledger summary."""
    s = get_settings()
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        ctx = _header(conn, job)
        rows = q.accounts(conn, job["id"])
    accounts = _account_rows(rows, s)
    if wants_json(request):
        return JSONResponse(jsonable_encoder({
            "job": {k: job[k] for k in ("id", "client_id", "status", "stop_optional",
                                        "created_at", "elapsed_s", "limits")},
            "ledger": ctx["ledger"],
            "accounts": [{"id": a["row"]["id"], "name": a["row"]["name"],
                          "decision": a["decision"]["decision"],
                          "decision_reason": a["decision"]["reason"], "score": a["score"],
                          "reasons": a["reasons"], "trust": a["trust"]} for a in accounts],
        }))  # fmt: skip
    return page(request, "accounts.html", accounts=accounts,
                counts=views.summary_counts(rows), **ctx)  # fmt: skip


# ---- 4 · evidence drawer ------------------------------------------------------------------
@app.get("/jobs/{job_id}/accounts/{account_id}")
def account_detail(request: Request, job_id: str, account_id: str) -> Response:
    """Every Fact with its sources, plus the audit log for that account."""
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        try:
            row = q.account(conn, job["id"], account_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="no such account") from exc
        if row is None:
            raise HTTPException(status_code=404, detail="no such account")
        audit_rows = q.account_audit(conn, job["id"], row["id"])
        ctx = _header(conn, job)
    data = row["data"] or {}
    facts = views.facts_of(data)
    if wants_json(request):
        return JSONResponse(jsonable_encoder({
            "account": {k: row[k] for k in ("id", "name", "domain", "status", "status_reason")},
            "facts": {f.key: data[f.key] for f in facts}, "unchecked": views.unchecked(data),
            "score": data.get("score"), "score_reasons": data.get("score_reasons") or [],
            "audit": audit_rows,
        }))  # fmt: skip
    name = "_evidence.html" if is_htmx(request) else "account.html"
    return page(request, name, account=row, data=data, facts=facts,
                unchecked=views.unchecked(data), decision=views.decision(row),
                audit=audit_rows, **ctx)  # fmt: skip


# ---- 5 · approval queue -------------------------------------------------------------------
@app.get("/approvals")
def approvals(request: Request) -> Response:
    with db.tx() as conn:
        cards = q.pending_approvals(conn)
    if wants_json(request):
        return JSONResponse(jsonable_encoder([{
            "action_key": c["idem_key"], "job_id": c["job_id"], "account": c["account_name"],
            "draft": c["draft_text"], "why": (c["context"] or {}).get("score_reasons", []),
            "facts": (c["context"] or {}).get("facts", []),
        } for c in cards]))  # fmt: skip
    return page(request, "approvals.html", cards=cards)


async def decide(key: str, action: str, to: str | None, subject: str | None, body: str | None,
                 reason: str | None, by_user: str) -> dict[str, Any]:  # fmt: skip
    """Move the action through the outbox, then learn from the decision."""
    reason = (reason or "").strip() or None
    with db.tx() as conn:
        card = q.approval_card(conn, key)
    if card is None:
        raise HTTPException(status_code=404, detail="no such draft")
    p = card["payload"]
    subject, body = (subject or p["subject"]).strip(), (body or p["body"]).strip()
    try:
        with db.tx() as conn:
            if action == "approve":
                if not (to and to.strip()):
                    raise ValueError("enter the recipient's business email address to approve")
                payload = {"to": [to.strip()], "subject": subject, "body": body}
                outbox.approve(conn, key, by_user, payload=payload, reason=reason)
            elif action == "reject":
                if not reason:
                    raise ValueError("a rejection needs a reason")
                outbox.reject(conn, key, by_user, reason)
            else:
                raise ValueError("action must be approve or reject")
    except outbox.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail="this draft was already decided") from exc
    except ValidationError as exc:
        msg = "; ".join(e["msg"] for e in exc.errors())
        raise HTTPException(status_code=422, detail=msg) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    decision = "approved" if action == "approve" else "rejected"
    final = draft_text(subject, body) if decision == "approved" else None
    out: dict[str, Any] = {"action_key": key, "state": decision}
    try:
        out["feedback"] = await learning.record_feedback(
            key, decision, final, reason, by_user=by_user, deps=learning_deps()
        )
    except Exception as exc:  # the decision stands; only the learning example is missing
        log.warning("feedback not stored", exc_info=True)
        out["feedback"] = {"recorded": False, "reason": f"{type(exc).__name__}: {exc}"}
    return out


@app.post("/approvals/{key:path}")
async def decide_approval(request: Request, key: str) -> Response:
    """Approve (as drafted or edited) or reject with a reason. Form or JSON."""
    s = get_settings()
    as_json = request.headers.get("content-type", "").startswith("application/json")
    f = await request.json() if as_json else dict(await request.form())
    action = f.get("action") or {"approved": "approve", "rejected": "reject"}.get(
        f.get("decision", ""), ""
    )
    try:
        out = await decide(key, action, f.get("to"), f.get("subject"), f.get("body"),
                           f.get("reason"), f.get("by_user") or s.outreach.approver)  # fmt: skip
    except HTTPException as exc:
        if is_htmx(request):
            with db.tx() as conn:
                card = q.approval_card(conn, key)
            return page(request, "_approval_card.html", c=card, error=exc.detail, form=f)
        raise
    if is_htmx(request):
        return page(request, "_decided_card.html", out=out, key=key)
    if as_json or wants_json(request):
        return JSONResponse(jsonable_encoder(out))
    return RedirectResponse("/approvals", status_code=HTTP_SEE_OTHER)


# ---- learned rules ------------------------------------------------------------------------
@app.get("/preferences")
def get_preferences(request: Request) -> Response:
    """Every rule version with its support, source diffs and status."""
    rows = learning.preferences()
    if wants_json(request):
        return JSONResponse(jsonable_encoder(rows))
    return page(request, "preferences.html", rules=rows, metric=learning.metric())


@app.post("/preferences/{pref_id}/{op}")
async def change_preference(request: Request, pref_id: int, op: str) -> Response:
    form = dict(await request.form()) if "form" in request.headers.get("content-type", "") else {}
    by = form.get("by_user") or get_settings().outreach.approver
    fns = {"disable": learning.disable_rule, "enable": learning.enable_rule,
           "rollback": learning.rollback_rule}  # fmt: skip
    if op not in fns:
        raise HTTPException(status_code=404, detail="unknown operation")
    try:
        row = fns[op](pref_id, by)
    except learning.FeedbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if is_htmx(request) or not wants_json(request):
        return RedirectResponse("/preferences", status_code=HTTP_SEE_OTHER)
    return JSONResponse(jsonable_encoder(row))


@app.post("/reflect")
async def run_reflect() -> dict[str, Any]:
    """reflect() on demand (it also runs after every outreach.reflect_every decisions)."""
    return await learning.reflect(deps=learning_deps())


@app.get("/learning/metric")
def learning_metric() -> list[dict[str, Any]]:
    """Mean normalized edit distance draft -> final per batch: the "it learns" chart."""
    return learning.metric()


# ---- 6 · outcome, output ------------------------------------------------------------------
@app.get("/jobs/{job_id}/output")
def job_output(job_id: str) -> JSONResponse:
    """The final client output (app/output/render.py)."""
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        return JSONResponse(jsonable_encoder(render_job(job["id"], conn=conn)))


@app.get("/jobs/{job_id}/outcome", response_class=HTMLResponse)
def job_outcome(request: Request, job_id: str) -> HTMLResponse:
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        ctx = _header(conn, job)
        actions = q.job_actions(conn, job["id"])
        output = render_job(job["id"], conn=conn)
    emails = [a for a in actions if a["type"] == "email"]
    meetings = [a for a in actions if a["type"] == "calendar"]
    return page(request, "outcome.html", emails=emails, meetings=meetings, output=output, **ctx)


# ---- 7 · run log --------------------------------------------------------------------------
@app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
def job_log(request: Request, job_id: str, step: str | None = None,
            endpoint: str | None = None) -> HTMLResponse:  # fmt: skip
    s = get_settings()
    step, endpoint = step or None, endpoint or None
    with db.tx() as conn:
        job = _job_or_404(conn, job_id)
        ctx = _header(conn, job)
        audit_rows = q.job_audit(conn, job["id"], step, s.api.log_page)
        ledger_rows = q.ledger_rows(conn, job["id"], endpoint, s.api.log_page)
        steps = q.audit_steps(conn, job["id"])
    name = "_log_tables.html" if is_htmx(request) else "log.html"
    groups = sorted({e["endpoint"].split(":")[0] for e in ctx["ledger"]["by_endpoint"]})
    return page(request, name, audit=audit_rows, ledger_rows=ledger_rows, steps=steps,
                step=step, endpoint=endpoint, groups=groups, **ctx)  # fmt: skip
