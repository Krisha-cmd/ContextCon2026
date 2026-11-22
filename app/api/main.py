"""HTTP API. /health, plus the SpecInvalid -> 422 handler shown to requesters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db
from app.config import get_settings
from app.log import get_logger, setup_logging
from app.outreach import learning
from app.spec.parser import SpecInvalid

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log.level)
    db.init_db(settings)
    log.info("api started")
    yield
    db.close_pool()


app = FastAPI(title="revagent", lifespan=lifespan)


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


# ---- approvals and the learning loop -------------------------------------------------------
class Decision(BaseModel):
    decision: str  # outreach.decisions: approved | rejected
    final_text: str | None = None  # the edited email ("Subject: ...\n\nbody"); None = as drafted
    reason: str | None = None
    by_user: str


class ChangedBy(BaseModel):
    by_user: str


@app.post("/approvals/{action_key}")
async def decide_approval(action_key: str, body: Decision) -> dict[str, Any]:
    """A person decided a draft: record it (example + diff + embedding), maybe reflect."""
    try:
        return await learning.record_feedback(
            action_key, body.decision, body.final_text, body.reason, by_user=body.by_user
        )
    except learning.FeedbackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/preferences")
def get_preferences() -> list[dict[str, Any]]:
    """Every rule version with its support, source diffs and status."""
    return jsonable_encoder(learning.preferences())


@app.post("/preferences/{pref_id}/disable")
def disable_preference(pref_id: int, body: ChangedBy) -> dict[str, Any]:
    try:
        return jsonable_encoder(learning.disable_rule(pref_id, body.by_user))
    except learning.FeedbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/preferences/{pref_id}/rollback")
def rollback_preference(pref_id: int, body: ChangedBy) -> dict[str, Any]:
    try:
        return jsonable_encoder(learning.rollback_rule(pref_id, body.by_user))
    except learning.FeedbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/reflect")
async def run_reflect() -> dict[str, Any]:
    """reflect() on demand (it also runs after every outreach.reflect_every decisions)."""
    return await learning.reflect()


@app.get("/learning/metric")
def learning_metric() -> list[dict[str, Any]]:
    """Mean normalized edit distance draft -> final per batch: the "it learns" chart."""
    return learning.metric()
