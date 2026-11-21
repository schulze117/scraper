"""A sweep cut into slices of pages, one GitHub job per slice.

    python -m find.shards plan   --portal immowelt --category HAUS_KAUFEN,HAUS_MIETEN --out plan.json
    python -m find.shards shard  --portal immowelt --category HAUS_KAUFEN --location 0 --first 2 --last 16 --out r.json
    python -m find.shards finish --plan plan.json --results results/

Two portals, two reasons, one mechanism:

  immowelt   DataDome walls an IP after roughly 30-45 search pages, and since
             2026-11-18 the home IP too. A GitHub runner gets a new address per
             job, so slices of 15 pages, ten in parallel, finish before any wall.
  immoscout  Only the home IP clears the WAF, so the slices run one at a time
             through the proxy. What slicing buys here is a time limit per slice
             instead of one for the whole group: a group that grows with the
             search area becomes more slices, never a job GitHub kills at 6 h.

Three steps, one `sweep_run` row:

  plan    starts the row (its started_at is the cycle start reconcile.py reads),
          fetches page 1 of every category/location -- the only page that knows
          the page count -- and cuts pages 2..N into slices.
  shard   walks one slice, retries its failed pages once, and writes a small JSON
          result. Exit 3 when the slice is incomplete.
  finish  adds plan and slices up and closes the row. `complete` only when page
          1 was read everywhere and every planned slice up to the end of the
          dated results came back complete -- a slice that never reported counts
          as failed.

Neighbouring slices overlap by one page. The portal sorts newest first, so new
listings arriving during the sweep push older ones one page deeper; the overlap
keeps a listing on a boundary from falling between two slices. The last slice of
a category follows the page count it sees, so a result set that grew since the
plan is still walked to its end.

The undated tail (immoscout only, see BaseFinder.RESULT_TAIL_*) is confirmed by
the slice that meets it -- three tail pages running, walking past its own last
page if it has to -- and *believed* only by finish, which alone sees the whole
category: the tail must start past the depth floor, and the slices before it must
have harvested at least that many dated pages. Slices that lie behind the tail
meet it on their first page and stop after three.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from find.base import BaseFinder
from find.immoscout import ImmoscoutFinder
from find.immowelt import ImmoweltFinder
from lib.database import Database
from lib.logger import get_logger

FINDERS: dict[str, type[BaseFinder]] = {
    "immowelt": ImmoweltFinder,
    "immoscout": ImmoscoutFinder,
}

# Pages per slice.
#   immowelt   well under the ~30 pages at which the lower DataDome tier walls
#              some runners; about 15 x 20 s plus a minute of setup.
#   immoscout  about 100 x 26 s, so 45-60 min, far inside the slice's job limit
#              even with a run of stalled fetches.
SHARD_PAGES = {"immowelt": 15, "immoscout": 100}

logger = get_logger("shards")


def _finder(portal: str, categories: set[str]) -> BaseFinder:
    finder = FINDERS[portal]()
    finder.only_categories = categories
    finder.sweep = True
    finder.MAX_PAGES = None
    # immowelt: each slice has its own IP, and the home proxy is exactly what
    # this replaces. immoscout: already on the proxy, there is nothing behind it.
    finder.PROXY_FALLBACK_IN_SWEEP = False
    return finder


def _categories(arg: str) -> set[str]:
    return {c.strip().upper() for c in arg.split(",") if c.strip()}


def plan(args) -> int:
    finder = _finder(args.portal, _categories(args.category))
    shard_pages = args.shard_pages or SHARD_PAGES[args.portal]
    categories = finder.select_categories()
    run_id = finder.db.start_sweep_run(finder.SOURCE, sorted(n.value for n, _ in categories))

    shards: list[dict] = []
    advertised: dict[str, int] = {}
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
            advertised[f"{name.value}/{index}"] = pages
            for first in range(2, pages + 1, shard_pages):
                last = min(first + shard_pages - 1, pages)
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
        {"run_id": str(run_id), "portal": args.portal, "advertised": advertised,
         "shards": shards, "result": result}, indent=1))
    logger.info(f"Sweep {run_id}: {len(shards)} slices of up to {shard_pages} pages"
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
    dated_pages = 0
    tail_streak = 0
    tail_at: int | None = None
    while page <= last:
        pages_count, new_count, is_tail = finder.process_page_strategy(category, location, page)
        if is_tail:
            # Neither a failure nor a listing page. Whether it is the real end of
            # the results only finish can judge; this slice only confirms it.
            tail_streak += 1
            if tail_streak >= finder.RESULT_TAIL_PAGES_TO_STOP:
                tail_at = page - tail_streak + 1
                break
            if page == last:
                # A streak open at the slice boundary is confirmed here rather than
                # left half-seen for the next slice.
                last += 1
        elif new_count is None:
            tail_streak = 0
            failures += 1
            if failures >= finder.SWEEP_FAILURES_TO_ABORT:
                hard_incomplete = True
                detail.append(f"aborted at page {page}")
                break
        else:
            tail_streak = 0
            failures = 0
            dated_pages += 1
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
        "category": name.value,
        "location": args.location,
        "first": args.first,
        "dated_pages": dated_pages,
        "tail_at": tail_at,
        "pages_ok": finder._pages_ok,
        "pages_failed": finder._pages_failed,
        "complete": complete,
        "detail": detail,
    }, indent=1))
    logger.info(f"Slice {name.value}/{args.location} pages {args.first}-{last}: "
                f"complete={complete}, {finder._pages_ok} ok, {finder._pages_failed} failed"
                + (f", tail from page {tail_at}" if tail_at else ""))
    return 0 if complete else 3


def judge(plan_data: dict, results: list[dict]) -> tuple[bool, list[str]]:
    """Is the sweep complete, and what goes into its detail?

    Per category/location: every planned slice in front of the end of the dated
    results must have reported complete. Where a slice met the undated tail, the
    tail is believed only under BaseFinder's two conditions, applied to the whole
    category rather than one slice: it starts past the depth floor, and the
    slices in front of it harvested at least that many dated pages. Slices behind
    a believed tail hold nothing we store and do not count either way.
    """
    finder_cls = FINDERS[plan_data["portal"]]
    complete = plan_data["result"]["complete"]
    detail = [f"{len(plan_data['shards'])} slices"] + list(plan_data["result"]["detail"])

    by_key = {(r["category"], r["location"], r["first"]): r for r in results}
    groups: dict[str, list[dict]] = {}
    for s in plan_data["shards"]:
        groups.setdefault(f"{s['category']}/{s['location']}", []).append(s)

    for key, planned in groups.items():
        planned.sort(key=lambda s: s["first"])
        reported = [by_key.get((s["category"], s["location"], s["first"])) for s in planned]
        tails = [r["tail_at"] for r in reported if r and r["tail_at"]]
        tail_at = min(tails) if tails else None

        if tail_at is not None:
            pages = plan_data["advertised"][key]
            floor = max(finder_cls.RESULT_TAIL_PAGES_TO_STOP,
                        int(pages * finder_cls.RESULT_TAIL_MIN_DEPTH_SHARE))
            before = [r for s, r in zip(planned, reported) if s["first"] < tail_at]
            # Page 1 came from the plan; neighbouring slices share one page.
            dated = 1 + sum(r["dated_pages"] for r in before if r) - max(len(before) - 1, 0)
            if tail_at >= floor and dated >= floor:
                detail.append(f"tail at page {tail_at}/{pages} for {key}")
            else:
                complete = False
                detail.append(f"tail at page {tail_at}/{pages} for {key} not believed "
                              f"({dated} dated pages, floor {floor})")
                tail_at = None

        for s, r in zip(planned, reported):
            if tail_at is not None and s["first"] >= tail_at:
                continue
            span = f"{key} {s['first']}-{s['last']}"
            if r is None:
                complete = False
                detail.append(f"{span}: never reported")
            elif not r["complete"]:
                complete = False
                detail.append(f"{r['slice']}: {'; '.join(r['detail']) or 'incomplete'}")

    return complete, detail


def finish(args) -> int:
    plan_data = json.loads(Path(args.plan).read_text())
    results = [json.loads(p.read_text()) for p in sorted(Path(args.results).glob("*.json"))]

    complete, detail = judge(plan_data, results)
    pages_ok = sum(p["pages_ok"] for p in (plan_data["result"], *results))
    pages_failed = sum(p["pages_failed"] for p in (plan_data["result"], *results))

    Database().finish_sweep_run(plan_data["run_id"], complete, pages_ok, pages_failed,
                                "; ".join(detail))
    logger.info(f"Sweep {plan_data['run_id']}: complete={complete}, {pages_ok} pages ok, "
                f"{pages_failed} failed — {'; '.join(detail)}")
    return 0 if complete else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--portal", choices=FINDERS, required=True)
    p.add_argument("--category", required=True)
    p.add_argument("--shard-pages", type=int, help="default: SHARD_PAGES of the portal")
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

    args = parser.parse_args(argv)
    return {"plan": plan, "shard": shard, "finish": finish}[args.step](args)


if __name__ == "__main__":
    sys.exit(main())
