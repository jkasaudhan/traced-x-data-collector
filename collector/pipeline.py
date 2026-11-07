"""Orchestration: plan -> fetch -> attribute -> store, with a budget guard."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from .attribution import classify
from .config import CaseConfig
from .db import Database
from .models import ContentItem
from .platforms import get_adapter
from .platforms.base import PlatformAdapter

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    specs_planned: int = 0
    specs_executed: int = 0
    items_returned: int = 0
    items_inserted: int = 0
    est_cost_usd: float = 0.0
    stopped_on_budget: bool = False


def _mentions_subject(item: ContentItem, case: CaseConfig) -> Optional[bool]:
    if not item.text:
        return None
    hay = item.text.lower()
    return any(t.lower() in hay for t in case.about_terms) if case.about_terms else None


def collect(db: Database, case: CaseConfig, case_id: UUID, platform: str,
            token: str, *, dry_run: bool = False,
            tiers: Optional[list[str]] = None,
            accounts: Optional[list[str]] = None,
            max_queries: Optional[int] = None,
            redo: bool = False) -> RunReport:
    options = case.platforms.get(platform, {})
    adapter: PlatformAdapter = get_adapter(platform, token, options)

    specs = adapter.plan(case)
    if tiers:
        specs = [s for s in specs if s.tier in tiers]
    if accounts:
        wanted = {f"from:{a.lstrip('@').lower()} " for a in accounts}
        specs = [s for s in specs
                 if s.tier != "subject_own" or any(s.query.startswith(w) for w in wanted)]
    if not dry_run and not redo:
        done = db.succeeded_queries(case_id, platform)
        skipped = sum(1 for s in specs if s.query in done)
        specs = [s for s in specs if s.query not in done]
        if skipped:
            log.info("skipping %d queries already collected (use --redo to repeat)", skipped)
    if max_queries is not None:
        specs = specs[:max_queries]
    report = RunReport(specs_planned=len(specs))

    already_spent = db.spent_usd(case_id)
    budget_left = case.budget_usd - already_spent
    log.info("planned %d queries | budget $%.2f, already spent $%.2f",
             len(specs), case.budget_usd, already_spent)

    if dry_run:
        for s in specs:
            print(f"[{s.tier:<12}] max={s.max_items:<5} {s.query}")
        worst = sum(s.max_items for s in specs) / 1000 * adapter.price_per_1k
        print(f"\n{len(specs)} queries | worst-case cost "
              f"${worst:.2f} at ${adapter.price_per_1k}/1k | budget ${budget_left:.2f} left")
        return report

    accounts = db.load_subject_accounts(case_id, platform)

    for spec in specs:
        if budget_left <= 0:
            log.warning("budget exhausted — stopping with %d queries unexecuted",
                        len(specs) - report.specs_executed)
            report.stopped_on_budget = True
            break

        # Never let a single query spend more than the budget that remains.
        affordable = int(budget_left / adapter.price_per_1k * 1000)
        capped = min(spec.max_items, max(affordable, 0))
        if capped <= 0:
            report.stopped_on_budget = True
            break
        spec.max_items = capped

        run_id = db.start_run(case_id, platform, spec.tier, adapter.actor_id,
                              spec.query, spec.slice_start, spec.slice_end,
                              spec.max_items)
        returned = inserted = 0
        try:
            for item in adapter.fetch(spec):
                returned += 1
                # Billed either way, but never store a post outside the slice.
                if item.posted_at and not (spec.slice_start <= item.posted_at < spec.slice_end):
                    log.warning("dropping %s: posted %s outside slice %s..%s",
                                item.platform_item_id, item.posted_at,
                                spec.slice_start, spec.slice_end)
                    continue
                attribution, reason = classify(item, accounts)

                # Learn the registered account's numeric id the first time we
                # see it, so later attribution no longer relies on the handle.
                if (attribution in ("subject_own", "subject_affiliated")
                        and item.author and item.author.platform_user_id
                        and item.author.handle):
                    for acct in accounts:
                        if acct.handle == item.author.handle and not acct.platform_user_id:
                            db.learn_account_id(case_id, platform, acct.handle,
                                                item.author.platform_user_id)
                            acct.platform_user_id = item.author.platform_user_id

                if db.upsert_item(case_id, item, attribution, reason, run_id,
                                  _mentions_subject(item, case)):
                    inserted += 1
            db.conn.commit()
            cost = returned / 1000 * adapter.price_per_1k
            db.finish_run(run_id, status="succeeded", items_returned=returned,
                          items_inserted=inserted, est_cost_usd=cost,
                          external_run_id=adapter.last_external_run_id())
        except Exception as exc:  # noqa: BLE001 - one bad slice must not kill the run
            db.conn.rollback()
            cost = returned / 1000 * adapter.price_per_1k
            db.finish_run(run_id, status="failed", items_returned=returned,
                          items_inserted=inserted, est_cost_usd=cost,
                          external_run_id=adapter.last_external_run_id(),
                          error=str(exc)[:2000])
            log.error("query failed (%s): %s", spec.query, exc)

        if returned >= spec.max_items:
            log.warning("[%s] hit the %d-item cap — this slice is probably truncated: %s",
                        spec.tier, spec.max_items, spec.query)
        budget_left -= cost
        report.specs_executed += 1
        report.items_returned += returned
        report.items_inserted += inserted
        report.est_cost_usd += cost
        log.info("[%s] %-60s -> %d items (+%d new) | $%.3f | $%.2f left",
                 spec.tier, spec.query[:60], returned, inserted, cost, budget_left)

    return report
