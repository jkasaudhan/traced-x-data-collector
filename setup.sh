#!/usr/bin/env bash
# One-command setup without Docker. Safe to re-run.
#
#   ./setup.sh
#
# Creates a Python 3.12 venv in .venv (via uv), installs dependencies, creates
# the database on your local Postgres and applies the schema.

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
fail() { printf '\033[31m    %s\033[0m\n' "$1"; }

# --- config -----------------------------------------------------------------
say "Checking .env"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    created .env from .env.example"
    warn "Add your APIFY_TOKEN to .env before collecting anything."
else
    echo "    .env already exists, leaving it alone"
fi

# --- venv -------------------------------------------------------------------
say "Setting up .venv"
if command -v uv >/dev/null; then
    [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
        || uv venv --python 3.12 --clear .venv
    uv pip install --quiet --python .venv/bin/python -r requirements-dev.txt
else
    [ -x .venv/bin/python ] || python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements-dev.txt
fi
PY=.venv/bin/python
echo "    $($PY --version)"

# --- database ---------------------------------------------------------------
say "Reaching your Postgres"
if ! $PY scripts/ensure_db.py; then
    fail "Could not reach Postgres — check DATABASE_URL_LOCAL in .env"
    fail "(should point at localhost) and that Postgres is running."
    exit 1
fi

say "Applying the schema"
$PY -m collector.cli init-db

say "Ready"
cat <<'EOT'
    make plan      # every query and the worst-case cost — spends nothing
    make check     # confirm database, schema and Apify token
    make collect   # collect the subject's own posts
    make help      # everything else
EOT
