.DEFAULT_GOAL := help
CASE ?= cases/mamdani.yaml

# Everything runs from the local venv (.venv) against your local Postgres.
PY   := .venv/bin/python
CLI  := $(PY) -m collector.cli
PSQL ?= $(shell command -v psql || echo /Applications/Postgres.app/Contents/Versions/latest/bin/psql)

.PHONY: help setup build psql init init-cloud test check plan \
        collect collect-mentions status export copy dump restore sqlite web web-cloud clean

help:  ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Override the case file with CASE=cases/other.yaml"

setup:  ## one-command setup: venv, database, schema
	@./setup.sh

build:  ## reinstall dependencies into the venv (after changing requirements)
	uv pip install --python $(PY) -r requirements-dev.txt

psql:  ## open psql against your local database
	@set -a; . ./.env; set +a; $(PSQL) "$$DATABASE_URL_LOCAL"

init:  ## apply the schema to your local database
	$(CLI) init-db

init-cloud:  ## apply the schema to Supabase
	$(CLI) --db cloud init-db

test:  ## run the test suite (no Apify calls, no spend)
	$(PY) -m pytest tests/ -q

check:  ## verify the database connection, schema and Apify token
	$(CLI) check

plan:  ## show every query and the worst-case cost (spends nothing)
	$(CLI) plan $(CASE)

collect:  ## collect the subject's own posts
	$(CLI) collect $(CASE) --tier subject_own

collect-mentions:  ## collect what everyone else posted
	$(CLI) collect $(CASE) --tier mentions

status:  ## per-attribution counts and spend so far
	$(CLI) status $(CASE)

export:  ## dump the case to out.jsonl in this directory
	$(CLI) export $(CASE) --out out.jsonl

copy:  ## promote the case from local to Supabase
	$(CLI) copy $(CASE) --from local --to cloud

dump:  ## dump the local disinfo database to dumps/disinfo-<date>.sql.gz
	@scripts/dump_db.sh

sqlite:  ## export the local disinfo database to dumps/disinfo-<date>.sqlite
	@$(PY) scripts/export_sqlite.py

restore:  ## restore a dump: make restore DUMP=dumps/x.sql.gz [URL=postgresql://...]
	@[ -n "$(DUMP)" ] || { echo "usage: make restore DUMP=dumps/disinfo-YYYYMMDD.sql.gz [URL=...]"; exit 2; }
	@scripts/restore_db.sh "$(DUMP)" $(URL)

web:  ## run the evidence viewer on http://127.0.0.1:8000 (local DB)
	@[ -x webapp/.venv/bin/python ] || (cd webapp && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt)
	cd webapp && .venv/bin/python -m app.main

web-cloud:  ## run the evidence viewer against Supabase
	@[ -x webapp/.venv/bin/python ] || (cd webapp && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt)
	cd webapp && .venv/bin/python -m app.main --db cloud

clean:  ## remove local caches
	rm -rf .pytest_cache **/__pycache__ __pycache__
