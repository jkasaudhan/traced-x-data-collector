# Evidence Viewer

A small read-only web app over the collector's database. It shows what the
subject **really posted** next to what **other accounts** posted about them,
so claims can be checked against the source and then on X itself.

- **Overview** — the subject's verified accounts, headline numbers, a daily
  timeline (real vs. others; click a day to compare it), the most-viewed posts
  by others, and the other accounts with the most reach.
- **Compare** — two columns with one shared search/date/media/sort filter:
  left is only `subject_own` + `subject_affiliated` (matched on X account ID),
  right is everyone else.
- **Explore** — one searchable feed: filter by source, post type
  (original / reply / quote), media, date range, account; sort by
  recency, views, likes, reposts, replies.
- **Post details** — why the post got its label (`attribution_reason`), author
  facts, full engagement, the post it quotes/replies to, how other accounts
  reacted to a real post (same thread / quoting it), the raw collected JSON,
  and a "View on X" link. Photos and videos play inline.

"Other account" means *not posted by the subject* — not that it is false.
The viewer is built to help a person make that call, not to make it for them.

## Run

```bash
cd webapp
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m app.main              # http://127.0.0.1:8000, local Postgres
.venv/bin/python -m app.main --db cloud   # read from Supabase instead
.venv/bin/python -m app.main --port 8080 --reload
```

or from the collector root: `make web` / `make web-cloud`.

Connection strings come from the collector's `../.env`
(`DATABASE_URL_LOCAL`, `DATABASE_URL_CLOUD`). Every connection is opened
read-only and all timestamps are served in UTC.

## API

```
GET /api/meta                  case, subject accounts, totals
GET /api/timeline?q=           posts per UTC day, real vs. others
GET /api/items                 side=all|real|others, q, start, end (inclusive),
                               media=any|with|photo|video|none,
                               kind=original|reply|quote, author (handle or id),
                               attribution, sort=recent|oldest|likes|views|reposts|replies,
                               limit (≤100), offset
GET /api/items/{id}            one post + parent + reactions + raw
GET /api/top-authors?side=     accounts ranked by total views
```

Interactive docs at `/docs`.

## Files

```
app/main.py      FastAPI routes and SQL
app/db.py        connection pool, reads ../.env
static/          index.html, app.js, styles.css — no build step
```
