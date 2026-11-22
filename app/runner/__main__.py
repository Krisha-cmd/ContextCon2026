"""CLI.

python -m app.runner run --request "find 3PL leads in Pune" [--client-id demo]
python -m app.runner run --request-file request.json
python -m app.runner resume <job_id>
python -m app.runner status <job_id>
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

from app import db
from app.config import get_settings
from app.log import setup_logging
from app.runner.jobs import create_job, job_status, resume_job
from app.runner.registry import REGISTRY, Registry
from app.runner.worker import Worker

DEFAULT_CLIENT = "demo-3pl"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="create a job and run it to completion")
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument("--request", help="the request text")
    src.add_argument("--request-file", type=Path, help="JSON file with the request/spec")
    run.add_argument("--client-id", default=DEFAULT_CLIENT)
    for name in ("resume", "status"):
        sub.add_parser(name).add_argument("job_id")
    return p


def load_spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.request is not None:
        return {"request": args.request}
    data = json.loads(args.request_file.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {"request": data}


def run_job(job_id: Any, registry: Registry) -> None:
    worker = Worker(get_settings(), registry, job_id=job_id)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, worker.request_stop)
    try:
        worker.run(until_job_done=True)
    finally:
        worker.close()


def print_status(job_id: Any, *, pretty: bool) -> None:
    """`status` pretty-prints; run/resume print one JSON line (stdout also carries log lines)."""
    with db.tx() as conn:
        print(json.dumps(job_status(conn, job_id), indent=2 if pretty else None, default=str))


def main(argv: list[str] | None = None, registry: Registry = REGISTRY) -> int:
    args = _parser().parse_args(argv)
    if registry is REGISTRY:
        import app.runner.pipeline  # noqa: F401  (registers the pipeline steps)
    settings = get_settings()
    setup_logging(settings.log.level)
    db.init_db(settings)
    try:
        if args.cmd == "status":
            print_status(args.job_id, pretty=True)
            return 0
        if args.cmd == "run":
            with db.tx() as conn:
                job_id = create_job(conn, settings, registry, load_spec(args), args.client_id)
            print(json.dumps({"job_id": str(job_id)}), file=sys.stderr)
        else:
            job_id = args.job_id
            with db.tx() as conn:
                resume_job(conn, settings, registry, job_id)
        run_job(job_id, registry)
        print_status(job_id, pretty=False)
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
