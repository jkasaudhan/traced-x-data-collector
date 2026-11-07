"""Command line entry point.

    python -m collector.cli init-db
    python -m collector.cli plan    cases/mamdani.yaml
    python -m collector.cli collect cases/mamdani.yaml --tier subject_own
    python -m collector.cli status  cases/mamdani.yaml
    python -m collector.cli export  cases/mamdani.yaml --out mamdani.jsonl

Every command runs against the LOCAL database unless told otherwise:

    python -m collector.cli --db cloud init-db
    python -m collector.cli --db cloud status cases/mamdani.yaml

Once the local data looks right, promote it:

    python -m collector.cli copy cases/mamdani.yaml --from local --to cloud
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import CaseConfig, Settings
from .db import Database
from .pipeline import collect


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="collector", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", choices=["local", "cloud"], default=None,
                   help="which database to use (default: DB_TARGET, else local)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-db", help="create tables")
    s.add_argument("--schema", default="schema.sql")

    s = sub.add_parser("check", help="verify the database connection and Apify token")

    s = sub.add_parser("copy", help="promote a case from one database to another")
    s.add_argument("case", help="path to the case YAML")
    s.add_argument("--from", dest="src", choices=["local", "cloud"], default="local")
    s.add_argument("--to", dest="dst", choices=["local", "cloud"], default="cloud")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    for name, helptext in [("plan", "show the queries without spending anything"),
                           ("collect", "run collection"),
                           ("status", "per-attribution counts"),
                           ("export", "dump the case to JSONL")]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("case", help="path to the case YAML")
        if name == "collect":
            s.add_argument("--platform", default="twitter")
            s.add_argument("--tier", action="append", dest="tiers",
                           help="limit to a tier; repeatable")
            s.add_argument("--account", action="append", dest="accounts",
                           help="limit subject_own to these handles; repeatable")
            s.add_argument("--max-queries", type=int, default=None,
                           help="stop after this many queries")
            s.add_argument("--redo", action="store_true",
                           help="re-run queries that already succeeded")
        if name == "plan":
            s.add_argument("--platform", default="twitter")
        if name == "export":
            s.add_argument("--out", required=True)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "copy":
        return _copy(args)
    if args.cmd == "check":
        return _check(args)

    settings = Settings.load(args.db)
    print(f"database: {settings.describe()}", file=sys.stderr)

    with Database(settings.database_url) as db:
        if args.cmd == "init-db":
            db.init_schema(args.schema)
            print("schema applied")
            return 0

        cfg = CaseConfig.load(args.case)
        case_id = db.upsert_case(cfg)

        if args.cmd == "plan":
            collect(db, cfg, case_id, args.platform, settings.apify_token, dry_run=True)
            return 0

        if args.cmd == "collect":
            try:
                token = settings.require_apify_token()
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 1
            r = collect(db, cfg, case_id, args.platform, token, tiers=args.tiers,
                        accounts=args.accounts,
                        max_queries=args.max_queries, redo=args.redo)
            print(f"\nexecuted {r.specs_executed}/{r.specs_planned} queries")
            print(f"items returned {r.items_returned}, new rows {r.items_inserted}")
            print(f"estimated spend ${r.est_cost_usd:.2f} "
                  f"(case total ${db.spent_usd(case_id):.2f} of ${cfg.budget_usd:.2f})")
            if r.stopped_on_budget:
                print("stopped early: budget limit reached")
            return 0

        if args.cmd == "status":
            rows = db.summary(case_id)
            if not rows:
                print("no items collected yet")
                return 0
            print(f"{'platform':<10} {'attribution':<22} {'n':>7}  window")
            for r in rows:
                lo = r["earliest"].date() if r["earliest"] else "?"
                hi = r["latest"].date() if r["latest"] else "?"
                print(f"{r['platform']:<10} {r['attribution']:<22} {r['n']:>7}  {lo} .. {hi}")
            print(f"\nspend so far: ${db.spent_usd(case_id):.2f}")
            return 0

        if args.cmd == "export":
            n = db.export_jsonl(case_id, args.out)
            print(f"wrote {n} rows to {args.out}")
            return 0

    return 1


def _check(args: argparse.Namespace) -> int:
    """Fail fast and legibly, rather than halfway through a paid run."""
    ok = True

    try:
        settings = Settings.load(args.db)
    except RuntimeError as exc:
        print(f"  config    FAIL  {exc}")
        return 1
    print(f"  config    ok    target = {settings.describe()}")

    try:
        with Database(settings.database_url) as db, db.conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()["version"].split(",")[0]
            cur.execute("""SELECT count(*) AS n FROM information_schema.tables
                           WHERE table_name = 'content_items'""")
            has_schema = cur.fetchone()["n"] > 0
        print(f"  database  ok    {version}")
        if has_schema:
            print("  schema    ok    tables present")
        else:
            print("  schema    FAIL  tables missing — run: make init")
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"  database  FAIL  {str(exc).strip().splitlines()[0]}")
        if "pooler.supabase.com" not in settings.database_url and \
                "supabase" in settings.database_url:
            print("                  use the Session pooler host, not db.<ref>.supabase.co")
        return 1

    token = settings.apify_token
    if not token:
        print("  apify     FAIL  APIFY_TOKEN is not set in .env")
        return 1
    if token.startswith("apify_api_xxxx") or token.startswith("dummy"):
        print("  apify     FAIL  APIFY_TOKEN is still the placeholder from .env.example")
        return 1
    try:
        from apify_client import ApifyClient
        me = ApifyClient(token).user("me").get()
        print(f"  apify     ok    signed in as {me.get('username', '?')} "
              f"(plan: {me.get('plan', '?')})")
    except Exception as exc:  # noqa: BLE001
        print(f"  apify     FAIL  {str(exc).strip().splitlines()[0]}")
        ok = False

    print("\nall good" if ok else "\nfix the FAILs above before collecting")
    return 0 if ok else 1


def _copy(args: argparse.Namespace) -> int:
    if args.src == args.dst:
        print("--from and --to are the same database; nothing to do")
        return 1

    cfg = CaseConfig.load(args.case)
    src_settings = Settings.load(args.src)
    dst_settings = Settings.load(args.dst)

    print(f"copying case {cfg.slug!r}")
    print(f"  from {src_settings.describe()}")
    print(f"    to {dst_settings.describe()}")
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    with Database(src_settings.database_url) as src, \
         Database(dst_settings.database_url) as dst:
        case_id = src.get_case_id(cfg.slug)
        if case_id is None:
            print(f"case {cfg.slug!r} not found in the source database")
            return 1
        dst.init_schema("schema.sql")      # safe to re-apply; all guards are IF NOT EXISTS
        moved = src.copy_case_to(dst, case_id)

    for table, n in moved.items():
        print(f"  {table:<18} {n:>7}")
    print("done — rows that already existed on the target were left untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
