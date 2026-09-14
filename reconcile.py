"""Deactivate what a complete sweep did not see.

The honest definition of offline: a listing the portal itself no longer returns
anywhere in its own search results. The sweep produces that set -- it walks every
page of every category -- so everything active that it did not touch is gone.

    python -m reconcile                    # dry run, every source
    python -m reconcile --apply
    python -m reconcile --source immowelt --apply

**The gate is the whole design.** Deactivating on absence is only sound if the
absence is real rather than a crawl that fell over, so a run counts only when:

  * it is recorded `complete` in `fixnflip_v2.sweep_run` -- every page of every
    category it claimed fetched successfully, last page reached. An exit code
    will not do: a sweep blocked at page 48 of 172 still exits 0.
  * EVERY category of the source has such a run. The oldest of those start times
    is the cycle start; before it, the whole inventory had been walked. A
    category that stops being swept blocks reconciliation for that source rather
    than letting its listings be declared dead by default.

What makes this safe to run often is that it is reversible: since 2026-09-13 a
sighting reactivates (`GENERAL_INSERT_SQL`), so a listing wrongly deactivated
here comes back by itself on the next sweep that sees it. That is why this
replaced the 21-day age rule in `expire.py`, which was deleted rather than kept
as a backstop — see CLAUDE.md for why a clock and a set difference must not both
be allowed to write this flag.
"""

import argparse
import sys

from lib.database import Database
from lib.logger import get_logger
from lib.models import (
    IMMOSCOUT_SEARCH_CATEGORIES,
    IMMOWELT_SEARCH_CATEGORIES,
    KLEINANZEIGEN_SEARCH_CATEGORIES,
    ListingSource,
)

# What a full cycle has to cover, per source.
CATEGORIES: dict[ListingSource, set[str]] = {
    ListingSource.IMMOBILIENSCOUT24: {c.value for c in IMMOSCOUT_SEARCH_CATEGORIES},
    ListingSource.IMMOWELT: {c.value for c in IMMOWELT_SEARCH_CATEGORIES},
    ListingSource.KLEINANZEIGEN: {c.value for c in KLEINANZEIGEN_SEARCH_CATEGORIES},
}

# A last line of defence, not the mechanism. The gate above is what makes this
# correct; this only catches a case where the gate is satisfied and the answer is
# still absurd -- a portal that changed its result layout so every page parsed to
# nothing, say. Raise it deliberately for the first run, which clears the backlog
# left from before anything cleared the flag at all.
MAX_DEACTIVATION_SHARE = 0.25

logger = get_logger("reconcile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", action="append", choices=[s.value for s in ListingSource],
                        help="Limit to this source (repeatable). Default: all.")
    parser.add_argument("--max-share", type=float, default=MAX_DEACTIVATION_SHARE,
                        help=f"Refuse above this share (default: {MAX_DEACTIVATION_SHARE}).")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write. Without it this is a dry run.")
    args = parser.parse_args(argv)

    sources = [s for s in ListingSource if not args.source or s.value in args.source]
    db = Database()
    blocked: list[str] = []
    total = 0

    for source in sources:
        wanted = CATEGORIES.get(source, set())
        cycle_start, covered, categories = db.sweep_coverage(source)

        missing = wanted - set(categories)
        if missing or not cycle_start:
            logger.warning(
                f"{source.value}: no complete sweep yet for "
                f"{', '.join(sorted(missing)) or 'any category'} — skipping. "
                f"Covered {covered}/{len(wanted)}."
            )
            blocked.append(source.value)
            continue

        unseen, active = db.count_unseen_since(source, cycle_start)
        if active == 0:
            logger.info(f"{source.value}: nothing active, skipping")
            continue

        share = unseen / active
        logger.info(
            f"{source.value}: cycle starts {cycle_start:%Y-%m-%d %H:%M}, all "
            f"{len(wanted)} categories covered. {unseen} of {active} active listings "
            f"were not seen in it ({share:.1%})."
        )

        if share > args.max_share:
            logger.error(
                f"{source.value}: REFUSING - {share:.1%} exceeds the {args.max_share:.0%} "
                f"ceiling. The sweeps report complete, so either the portal changed shape "
                f"or this is the one-off backlog. Look before overriding with --max-share."
            )
            blocked.append(source.value)
            continue

        if not args.apply:
            logger.info(f"{source.value}: dry run, would deactivate {unseen}. Pass --apply to write.")
            continue

        total += db.deactivate_unseen_since(source, cycle_start)

    if blocked:
        logger.error(f"Blocked or refused for {len(blocked)} source(s): {', '.join(blocked)}")
        return 1

    logger.info(f"Done. {total} listings deactivated." if args.apply
                else "Dry run complete, nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
