"""HTTP API. Only /health for now."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import db
from app.config import get_settings
from app.log import get_logger, setup_logging

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


@app.get("/health")
def health() -> JSONResponse:
    try:
        ok = db.ping()
    except Exception as exc:  # report, don't crash the probe
        log.warning("health db check failed", extra={"error": str(exc)})
        ok = False
    body = {"status": "ok" if ok else "degraded", "db": "ok" if ok else "down"}
    return JSONResponse(body, status_code=200 if ok else 503)
