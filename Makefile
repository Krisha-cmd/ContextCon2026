# Shortcuts. See README "Run it".
.PHONY: up down test lint deploy teardown kill-test

up:          ## local stack: postgres + api (:8000) + worker
	docker compose up -d --build

down:
	docker compose down

test:        ## unit + DB tests (needs the compose postgres for the DB ones)
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

deploy:      ## GCP: Cloud SQL + Secret Manager + Cloud Run service and job
	scripts/deploy.sh

teardown:    ## delete the Cloud SQL instance, service and job
	scripts/teardown.sh

kill-test:   ## hard-kill a worker mid-step / mid-send, restart, check nothing runs twice
	docker compose up -d postgres
	uv run pytest -v -o addopts="" -m "not live" \
	  "tests/test_runner.py::test_hard_kill_mid_step_then_restart" \
	  tests/test_actions_outbox.py -k "kill or crash or concurrent"
