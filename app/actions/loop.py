"""Outbox loop: runs execute_ready every actions.poll_interval_s next to the step runner.

It runs in its own thread with its own event loop and its own Gateway (async DB pool + httpx
client): psycopg async pools are bound to the loop that opened them, and a long step on the
runner thread must not delay sending approved actions.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import db
from app.actions.effects import Effects
from app.actions.outbox import default_owner, execute_ready
from app.config import Settings
from app.gateway import Gateway
from app.log import bind, get_logger

log = get_logger(__name__)


class OutboxLoop(threading.Thread):
    def __init__(self, settings: Settings, stop: threading.Event, owner: str | None = None) -> None:
        super().__init__(name="outbox", daemon=True)
        self.settings = settings
        self.stop_event = stop
        self.owner = owner or f"outbox:{default_owner()}"
        self.runs = 0

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _gateway(self) -> tuple[Gateway, AsyncConnectionPool, httpx.AsyncClient]:
        s = self.settings
        pool = AsyncConnectionPool(
            db.database_url(s),
            min_size=1,
            max_size=s.db.pool_max,
            open=False,
            kwargs={"row_factory": dict_row, "connect_timeout": s.db.connect_timeout_s},
        )
        await pool.open(wait=True)
        client = httpx.AsyncClient()
        return Gateway(s, pool, client), pool, client

    async def _main(self) -> None:
        gw, pool, client = await self._gateway()
        effects = Effects(self.settings, call=gw.call)
        interval = self.settings.actions.poll_interval_s
        with bind(step="outbox"):
            log.info("outbox loop started", extra={"owner": self.owner, "every_s": interval})
            try:
                while not self.stop_event.is_set():
                    await self.tick(effects)
                    await asyncio.to_thread(self.stop_event.wait, interval)
            finally:
                await client.aclose()
                await pool.close()
                log.info("outbox loop stopped", extra={"runs": self.runs})

    async def tick(self, effects: Effects) -> None:
        try:
            await execute_ready(effects, settings=self.settings, owner=self.owner)
        except Exception:  # keep looping; the failed batch is re-claimed after its lease
            log.warning("execute_ready failed", exc_info=True)
        self.runs += 1
