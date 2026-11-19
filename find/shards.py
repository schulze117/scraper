"""A sweep split over many runners, one fresh IP per slice of pages.

    python -m find.shards plan   --portal immowelt --category HAUS_KAUFEN,HAUS_MIETEN --out plan.json
    python -m find.shards shard  --portal immowelt --category HAUS_KAUFEN --location 0 --first 2 --last 16 --out r.json
    python -m find.shards finish --plan plan.json --results results/ --expected 14

Why: DataDome walls immowelt after roughly 30-45 search pages per IP. A single
sweep therefore had to move onto the home proxy at the wall, and on 2026-11-18
immowelt blocked the home IP too. A GitHub runner gets a new address per job, so
slices short enough to finish before the wall never meet it at all.

Three steps, one `sweep_run` row:

  plan    starts the row (its started_at is the cycle start reconcile.py reads),
          fetches page 1 of every category/location -- the only page that knows
          the page count -- and cuts pages 2..N into slices of SHARD_PAGES.
  shard   walks one slice direct, retries its failed pages once, and writes a
          small JSON result. Exit 3 when the slice is incomplete.
  finish  adds plan and slices up and closes the row. `complete` only when page
          1 was read everywhere and every planned slice came back complete --
          a slice that never reported counts as failed.

Neighbouring slices overlap by one page. The portal sorts newest first, so new
listings arriving during the sweep push older ones one page deeper; the overlap
keeps a listing on a boundary from falling between two slices. The last slice of
a category follows the page count it sees, so a result set that grew since the
plan is still walked to its end.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from find.base import BaseFinder
from find.immowelt import ImmoweltFinder
from lib.database import Database
from lib.logger import get_logger

FINDERS: dict[str, type[BaseFinder]] = {"immowelt": ImmoweltFinder}

# Pages per slice. Well under the ~30 pages at which the lower DataDome tier
# walls some runners; a slice costs about 15 x 20 s plus a minute of setup.
SHARD_PAGES = 15

logger = get_logger("shards")


def _finder(portal: str, categories: set[str]) -> BaseFinder:
    finder = FINDERS[portal]()
    finder.only_categories = categories
    finder.sweep = True
    finder.MAX_PAGES = None
    # Each slice has its own IP; there is nothing to fall back to and the home
    # proxy is exactly what this replaces.
    finder.PROXY_FALLBACK_IN_SWEEP = False
    return finder


def _categories(arg: str) -> set[str]:
    return {c.strip().upper() for c in arg.split(",") if c.strip()}


def plan(args) -> int:
    finder = _finder(args.portal, _categories(args.category))
    categories = finder.select_categories()
    run_id = finder.db.start_sweep_run(finder.SOURCE, sorted(n.value for n, _ in categories))

    shards: list[dict] = []
    lost: list[str] = []
    for name, category in categories:
        for index, location in enumerate(finder.get_locations()):
            pages = None
            for attempt in range(1, finder.PAGE_ONE_ATTEMPTS + 1):
                pages, new_count, _ = finder.process_page_strategy(category, location, page=1)
                if new_count is not None:
                    break
                pages = None
            if pages is None:
                lost.append(f"{name.value}/{index}")
                continue
            for first in range(2, pages + 1, args.shard_pages):
                last = min(first + args.shard_pages - 1, pages)
                shards.append({
                    "category": name.value,
                    "location": index,
                    # One page of overlap with the slice before, see the docstring.
                    "first": first - 1 if first > 2 else first,
                    "last": last,
                    "open_end": last == pages,
                })

    result = {
        "pages_ok": finder._pages_ok,
        "pages_failed": finder._pages_failed,
        "complete": not lost,
        "detail": [f"lost page 1: {', '.join(lost)}"] if lost else [],
    }
    Path(args.out).write_text(json.dumps(
        {"run_id": str(run_id), "shards": shards, "result": result}, indent=1))
    logger.info(f"Sweep {run_id}: {len(shards)} slices of up to {args.shard_pages} pages"
                + (f", page 1 lost for {', '.join(lost)}" if lost else ""))
    return 0


def shard(args) -> int:
    finder = _finder(args.portal, {args.category.upper()})
    (name, category), = finder.select_categories()
    location = finder.get_locations()[args.location]

    page, last = args.first, args.last
    failures = 0
    detail: list[str] = []
    hard_incomplete = False
    while page <= last:
        pages_count, new_count, is_tail = finder.process_page_strategy(category, location, page)
        if new_count is None:
            if is_tail:
                # Only immoscout raises the tail, and only a full sweep can judge
                # it. Here it is simply a page we did not read.
                hard_incomplete = True
                detail.append(f"tail page {page}")
            failures += 1
            if failures >= finder.SWEEP_FAILURES_TO_ABORT:
                hard_incomplete = True
                detail.append(f"aborted at page {page}")
                break
        else:
            failures = 0
            if args.open_end and pages_count > last:
                last = pages_count
            elif pages_count and pages_count < last:
                # The result set shrank since the plan; the pages behind its end
                # do not exist.
                last = max(pages_count, page)
        page += 1

    finder._retry_failed_pages()
    if finder._failed_pages:
        detail.append(f"{len(finder._failed_pages)} pages still failing after retry")
    complete = not hard_incomplete and not finder._failed_pages

    Path(args.out).write_text(json.dumps({
        "slice": f"{name.value}/{args.location} {args.first}-{last}",
        "pages_ok": finder._pages_ok,
        "pages_failed": finder._pages_failed,
        "complete": complete,
        "detail": detail,
    }, indent=1))
    logger.info(f"Slice {name.value}/{args.location} pages {args.first}-{last}: "
                f"complete={complete}, {finder._pages_ok} ok, {finder._pages_failed} failed")
    return 0 if complete else 3


def finish(args) -> int:
    plan_data = json.loads(Path(args.plan).read_text())
    results = [json.loads(p.read_text()) for p in sorted(Path(args.results).glob("*.json"))]

    parts = [plan_data["result"], *results]
    pages_ok = sum(p["pages_ok"] for p in parts)
    pages_failed = sum(p["pages_failed"] for p in parts)
    detail = [f"{args.expected} slices"] + list(plan_data["result"]["detail"])
    missing = args.expected - len(results)
    if missing > 0:
        detail.append(f"{missing} slices never reported")
    for r in results:
        if not r["complete"]:
            detail.append(f"{r['slice']}: {'; '.join(r['detail']) or 'incomplete'}")
    complete = plan_data["result"]["complete"] and missing <= 0 and all(r["complete"] for r in results)

    Database().finish_sweep_run(plan_data["run_id"], complete, pages_ok, pages_failed,
                                "; ".join(detail))
    return 0 if complete else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--portal", choices=FINDERS, required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--shard-pages", type=int, default=SHARD_PAGES)
    p.add_argument("--out", required=True)

    s = sub.add_parser("shard")
    s.add_argument("--portal", choices=FINDERS, required=True)
    s.add_argument("--category", required=True)
    s.add_argument("--location", type=int, required=True)
    s.add_argument("--first", type=int, required=True)
    s.add_argument("--last", type=int, required=True)
    s.add_argument("--open-end", action="store_true")
    s.add_argument("--out", required=True)

    f = sub.add_parser("finish")
    f.add_argument("--plan", required=True)
    f.add_argument("--results", required=True)
    f.add_argument("--expected", type=int, required=True)

    args = parser.parse_args(argv)
    return {"plan": plan, "shard": shard, "finish": finish}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
