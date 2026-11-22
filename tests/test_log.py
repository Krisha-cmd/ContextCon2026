from __future__ import annotations

import json
import logging

import pytest

from app.log import bind, get_logger, setup_logging


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_json_line_with_bound_fields(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO")
    log = get_logger("t")
    with bind(job_id="j1", account_id="a1"), bind(step="identity"):
        log.info("hello", extra={"credits": 0.2})
    log.info("outside")
    first, second = _lines(capsys)
    assert first["msg"] == "hello"
    assert (first["job_id"], first["account_id"], first["step"]) == ("j1", "a1", "identity")
    assert first["credits"] == 0.2
    assert first["level"] == "INFO"
    assert second["job_id"] is None and second["step"] is None


def test_exceptions_are_serialised(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO")
    try:
        raise ValueError("bad")
    except ValueError:
        get_logger("t").exception("failed")
    (line,) = _lines(capsys)
    assert "ValueError: bad" in line["exc"]


def test_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("WARNING")
    get_logger("t").info("hidden")
    assert _lines(capsys) == []
    logging.getLogger().setLevel("INFO")
