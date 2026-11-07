# Disinformation Evidence Collector

Case-based collection of public social content about a named public figure,
stored in Postgres with every item marked as **the subject's own post** or
**someone else's**. Twitter/X is implemented; the adapter interface is built so
TikTok, Instagram and YouTube slot in without touching anything else.

Built as the ingestion stage of a takedown-evidence tool: the attribution
column and the `collection_runs` provenance table exist so a later stage can
produce a defensible report, not just a dataset.

## Why it queries the way it does

X caps a user timeline at roughly the last 3,200 posts, so scraping a profile
cannot reach October 2025. Advanced search has no such ceiling, so the
collector slices the window and issues `from:handle since_time:A until_time:B`
per slice. The exact Unix-second operators are used because the date-level
`since:`/`until:` were observed to leak a day past `until`; anything that still
lands outside its slice is dropped before storage, and a warning is logged
whenever a slice hits its item cap (i.e. was probably truncated).
Same mechanism for third-party mentions, with an engagement floor to keep an
unbounded query affordable.

## Quickstart

Runs from a Python venv (`.venv`, Python 3.10+; `setup.sh` uses 3.12 via
`uv` when available) against your locally installed Postgres. No Docker.

```bash
./setup.sh
```

That creates `.venv`, installs dependencies, creates the `disinfo` database
on your Postgres and applies the schema. Safe to re-run. Then:

```bash
make plan      # every query and the worst-case cost — spends nothing
make check     # confirm database, schema and Apify token
make collect   # collect the subject's own posts
make help      # everything else
```

Add your `APIFY_TOKEN` to `.env` before collecting — `plan`, `status`,
`export` and `copy` work without it; `collect` does not. `.env` should point
at your local server:

```
DATABASE_URL_LOCAL=postgresql://postgres:postgres@localhost:5432/disinfo
```

Without make: `source .venv/bin/activate` and call
`python -m collector.cli ...` directly.

### Make targets

```
make setup            venv, dependencies, database, schema
make build            reinstall dependencies (after changing requirements)
make psql             psql against your local database
make check            verify database, schema and Apify token
make plan             show the query plan and cost
make collect          collect the subject's own posts
make collect-mentions collect what everyone else posted
make status           per-attribution counts and spend so far
make export           dump the case to out.jsonl
make dump             dump the local disinfo database to dumps/
make restore DUMP=…   restore a dump into an empty database
make sqlite           export the local disinfo database to a SQLite file
make init-cloud       create the tables on Supabase
make copy             promote the case local -> Supabase
make test             run the tests
make web              evidence viewer on :8000 (see webapp/README.md)
make web-cloud        same, reading from Supabase
make clean            remove caches
```

Point any of them at another case with `make plan CASE=cases/other.yaml`.

## Files

```
setup.sh              one-command setup
Makefile              shortcuts for everything below
requirements.txt      runtime dependencies (apify-client pinned to 1.x:
                      the code uses its dict-shaped responses)
requirements-dev.txt  the above plus pytest
.env.example          copy to .env
schema.sql            the database schema
scripts/ensure_db.py  creates the database if it does not exist
scripts/dump_db.sh    dump the local disinfo database (portable plain SQL, gzipped)
scripts/restore_db.sh restore a dump into an empty database
scripts/export_sqlite.py  export the local disinfo database to SQLite
dumps/                database dumps + .txt manifest (row counts, sha256)
cases/mamdani.yaml    the case definition — one file per subject
collector/            the package
tests/                stubbed end-to-end tests, no Apify calls
webapp/               read-only evidence viewer (FastAPI + static frontend)
```

## Local first, then cloud

Every command runs against the **local** database unless you say otherwise.
Cloud is never implicit — it takes an explicit `--db cloud` or `DB_TARGET=cloud`,
because the point of having two targets is that you can get it wrong locally
for free. Each command prints which database it is using, on stderr, before it
does anything. The URL itself is never printed — it contains the password.

### 1. Collect and review locally

```bash
make plan                 # queries and worst-case cost, spends nothing
make collect              # the subject's own accounts
make collect-mentions     # everyone else
make status               # per-attribution counts and spend
make psql                 # inspect the rows yourself
```

Only move on once the local data looks right.

### 2. Get the Supabase connection string

Project Settings → Database → Connection string → **Session pooler**:

```
postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Three things that cost people an afternoon:

- Use the pooler host, not `db.<ref>.supabase.co`. The direct host is
  IPv6-only and will not resolve on most home and office networks
  (`failed to resolve host 'db.<ref>.supabase.co'`).
- The username is `postgres.<project-ref>`, not `postgres`.
- URL-encode the password if it contains `@ : / ? #` or `%`.

If the dashboard only shows you the direct string, you can find the region
yourself: `dig +short AAAA db.<ref>.supabase.co` and look up the AWS region of
that IPv6 range (`2a05:d018:…` is `eu-west-1`), then try
`aws-0-<region>.pooler.supabase.com`. A wrong pooler host answers
`tenant/user postgres.<ref> not found`.

Put it in `.env` (never commit that file):

```
DATABASE_URL_CLOUD=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

The transaction pooler (port 6543) works too — `_connect_kwargs` disables
psycopg's prepared statements automatically when it sees a pooled host, which
is otherwise a failure that only shows up partway through a long run, after
the same query has been issued five times. Session mode is still the safer
default for batch inserts.

### 3. Check the connection and create the tables

```bash
.venv/bin/python -m collector.cli --db cloud check     # expect database ok, schema FAIL
make init-cloud                                        # = --db cloud init-db
.venv/bin/python -m collector.cli --db cloud check     # now schema ok
```

`init-db` is idempotent. `pgcrypto` and `pg_trgm` are created if the role is
allowed to; if not, the schema still applies and only the trigram indexes are
skipped.

### 4. Copy the case

```bash
make copy        # = copy cases/mamdani.yaml --from local --to cloud
```

It prints source and target and asks `proceed? [y/N]`. Non-interactively:
`echo y | .venv/bin/python -m collector.cli copy cases/mamdani.yaml --from local --to cloud`.

It copies `cases`, `subject_accounts`, `collection_runs`, `authors`,
`content_items` and `media_assets` with `ON CONFLICT DO NOTHING`, so it is
safe to re-run: after collecting more locally, run it again and only the new
rows move. Rows already on Supabase are never updated.

### 5. Verify

Run the same query on both sides and diff:

```bash
set -a; . ./.env; set +a
SQL="select 'content_items', count(*) from content_items
     union all select 'authors', count(*) from authors
     union all select 'media_assets', count(*) from media_assets
     union all select 'collection_runs', count(*) from collection_runs
     union all select attribution::text, count(*) from content_items group by 1
     order by 1"
diff <(psql "$DATABASE_URL_LOCAL" -Atc "$SQL") <(psql "$DATABASE_URL_CLOUD" -Atc "$SQL") && echo IDENTICAL
```

### Collecting straight into Supabase

Once you trust the pipeline you can skip the local step:

```bash
.venv/bin/python -m collector.cli --db cloud collect cases/mamdani.yaml --tier mentions
```

Budget spent is tracked per database, so a case collected partly locally
and partly in the cloud has its spend split across both — copy first if you
want the cloud's budget guard to see everything.

## Sharing the dataset (dump and restore)

The collected data ships as one file: `dumps/disinfo-<date>.sql.gz`, a
gzipped plain-SQL dump of the local `disinfo` database (schema, data,
indexes), with a `.txt` manifest beside it listing row counts and a sha256.
Nobody needs an Apify token or Supabase access to use it.

### Make a dump

```bash
make dump        # = scripts/dump_db.sh
```

It refuses to run if `DATABASE_URL_LOCAL` does not point at `disinfo`. The
dump is plain SQL with `--no-owner --no-privileges`, and the `\restrict` /
`SET transaction_timeout` lines that pg_dump 17+ adds are stripped, so it
restores into any Postgres 13 or newer with any psql.

### Restore it (for whoever receives the file)

Needs a running Postgres 13+ and `psql`, and nothing else from this repo
except the script (or not even that — see "by hand" below).

```bash
# into postgresql://postgres:postgres@localhost:5432/disinfo (created if missing)
scripts/restore_db.sh dumps/disinfo-20261107.sql.gz

# or anywhere else
scripts/restore_db.sh dumps/disinfo-20261107.sql.gz postgresql://me:secret@localhost:5432/disinfo

# same via make
make restore DUMP=dumps/disinfo-20261107.sql.gz
```

The script verifies the checksum against the manifest, creates the database
if needed, refuses to restore over a database that already has the tables,
loads everything in a single transaction (all or nothing) and prints the row
counts to compare with the manifest.

**By hand**, with only psql:

```bash
createdb disinfo
gunzip -c disinfo-20261107.sql.gz | psql -v ON_ERROR_STOP=1 --single-transaction -d disinfo
psql -d disinfo -c "select attribution, count(*) from content_items group by 1"
```

**No Postgres installed?** One throwaway container is enough:

```bash
docker run -d --name disinfo-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=disinfo -p 5432:5432 postgres:17
gunzip -c disinfo-20261107.sql.gz | docker exec -i disinfo-db psql -U postgres -d disinfo -v ON_ERROR_STOP=1 --single-transaction
```

Then point `DATABASE_URL_LOCAL` at it and `make web` to browse it in the
evidence viewer.

### SQLite copy (no database server needed)

For people who just want to open the data — in DB Browser for SQLite,
Datasette, DuckDB, pandas, or the `sqlite3` CLI — the same database is also
exported as one SQLite file:

```bash
make sqlite      # -> dumps/disinfo-<date>.sqlite (~40 MB)
```

Share `dumps/disinfo-<date>.sqlite` as is — it is a normal SQLite database,
usable directly with no import step:

```bash
sqlite3 dumps/disinfo-20261107.sqlite
```

Same tables and columns as Postgres. Timestamps are ISO-8601 UTC text,
booleans are 0/1, `raw`/`labels`/`config` are JSON text (use `json_extract`).
Extras inside the file:

- `v_posts` — one row per post with author fields and `is_real`
  (1 = posted by the subject's registered accounts).
- `posts_fts` — full-text search over post text and author, stemmed
  (`freeze` also finds `freezes`).
- `export_info` — when it was exported, row counts, and what each
  attribution label means.

```sql
-- real vs. others
SELECT is_real, count(*) FROM v_posts GROUP BY 1;

-- most-viewed posts by other accounts
SELECT author_handle, view_count, url, substr(text, 1, 80)
FROM v_posts WHERE is_real = 0 ORDER BY view_count DESC LIMIT 10;

-- search, then show who posted it
SELECT p.is_real, p.author_handle, p.posted_at, p.url
FROM posts_fts f JOIN v_posts p ON p.id = f.item_id
WHERE posts_fts MATCH '"rent freeze"' ORDER BY p.posted_at;

-- a field from the original scraper JSON
SELECT json_extract(raw, '$.author.username'), json_extract(raw, '$.urls')
FROM content_items LIMIT 5;
```

The exporter checks foreign keys and SQLite integrity before replacing the
output file, and writes to a temp file first so a failed run never leaves a
half-written database behind.

## Use

```bash
# See every query and the worst-case cost. Spends nothing.
python -m collector.cli plan cases/mamdani.yaml

# His own account first — cheap, bounded, and it teaches the pipeline
# the numeric user id that makes later attribution authoritative.
python -m collector.cli collect cases/mamdani.yaml --tier subject_own

# Then everyone else.
python -m collector.cli collect cases/mamdani.yaml --tier mentions

python -m collector.cli status cases/mamdani.yaml
python -m collector.cli export cases/mamdani.yaml --out mamdani.jsonl
```

`collect` skips any query that already succeeded in the target database, so
an interrupted run resumes where it stopped. Useful flags:

```
--max-queries N     stop after N queries (e.g. to top up to a row count)
--account HANDLE    limit subject_own to one account; repeatable
--redo              re-run queries that already succeeded
```

Tests run against the local Postgres with a stubbed actor — no Apify calls, no spend:

```bash
python -m pytest tests/ -q
```

## Attribution

`content_items.attribution` is one of:

| value | meaning |
|---|---|
| `subject_own` | author is a registered `personal` account of the subject |
| `subject_affiliated` | registered `campaign` / `office` / `staff` account |
| `impersonation_suspect` | handle resembles a registered one but the user id does not match, or matches a handle whose id is wrong |
| `third_party` | author resolved, not registered |
| `unknown` | no author data on the row |

Every row also carries `attribution_reason` — a sentence explaining the
decision, so a reviewer can audit it rather than trust it.

Two details that matter in practice:

**User id beats handle.** Handles get renamed and recycled. The first run
matches on handle, learns the account's numeric id from the matched post,
writes it back to `subject_accounts`, and every run after that matches on the
id. Paste the learned ids into the YAML once you have them.

**Office handles have validity windows.** The shipped case registers only the
one confirmed handle, `@ZohranKMamdani`. If you later add `@NYCMayor`, give it
`role: office` and `valid_from: 2026-01-01` — it belonged to the previous
administration until 2025-12-31, and your collection window straddles that
handover. A post from it dated November 2025 then classifies as `third_party`
with the window cited in the reason. Without `valid_from`, the collector would
credit the predecessor's posts to Mamdani — exactly the kind of error that
discredits an evidence file.

## Cost control

`budget_usd` in the case YAML is a hard ceiling. It ships at **$5.00**, matching
the Apify Free plan's monthly platform credits. Those credits **expire each
cycle and do not roll over**, so spend them inside the billing month. Before each query the
pipeline computes what is still affordable, shrinks that query's `maxItems` to
fit, and stops the run when nothing is left. Actual spend accumulates in
`collection_runs.est_cost_usd`, so `status` always shows the real total.

Costs are estimated as `items_returned / 1000 × price_per_1k`. Set
`price_per_1k` from the actor's own pricing page — if it changes and you
don't, the guard silently drifts.

Default config: weekly own-account slices (retweets excluded) plus 12-hour
mention slices (Latest — X's Top tab ignores the time operators), capped at
40 each — about **$3.9 worst
case** at $0.25/1k. Run `plan` to confirm before spending anything.

## Choosing an actor

| actor | price | notes |
|---|---|---|
| `jacquemus/x-tweet-scraper` | $0.25 / 1k (was $0.10 until 2026-09-15) | pay-per-event, works inside the Apify Free plan's $5 monthly credits. Default. |
| `apidojo/tweet-scraper` | ~$0.40 / 1k | mature and widely used, but the API is blocked on the Free plan and it enforces a 50-tweet minimum per query, which breaks thin weekly slices. |

Swap via `platforms.twitter.actor_id`. The output normaliser reads both camelCase
and snake_case field names, so a different actor usually needs no code change.

## Adding a platform

Subclass `PlatformAdapter` in `collector/platforms/`, implement `plan()` and
`fetch()`, decorate with `@register`, and import it in
`collector/platforms/__init__.py`. Set `platform` to a value in the
`platform_t` enum. Attribution, storage, budget and CLI all work unchanged.

## Schema

```
cases              one investigation (subject, window, about_terms, budget)
subject_accounts   registered genuine accounts — ground truth for attribution
collection_runs    one row per query: actor, query, items, cost, Apify run id
authors            one row per account seen, per platform
content_items      canonical post table + attribution + enrichment slots
media_assets       per-attachment, with sha256 / phash columns for evidence
```

`content_items.raw` keeps the untouched actor payload, so a schema change
later can be backfilled without re-paying for collection.

## Limits worth knowing before you demo this

- **Deleted content is unreachable.** Posts removed before collection — often
  the most important ones — are gone. Pair this with Wayback / archive.today.
- **Search results are a ranked subset**, not a census. Counts from this tool
  are "what search returned", not "what was posted". Say so in any report.
- **Scraping X is against its terms.** Fine for a hackathon; a real
  conversation before this ships to clients.
- `mentions_subject` is a naive substring check. Replace it with a real
  relevance classifier before you trust the third-party counts.
