"""Mark listings inactive that the finder has stopped returning.

Why this exists: nothing else ever sets `general.active = FALSE` for immoscout and
immowelt. The scrape step does deactivate a listing it finds deactivated, but both
portals only re-scrape when `modified_at` advances, so a listing that quietly goes
offline is never revisited and stays `active = TRUE` forever. On 2026-09-13 that
was 55% of everything the database called active.

`system.last_seen_at` is the signal that does track reality — the finder refreshes
it for every listing on every page it crawls. This job turns it into the flag.

    python -m expire                  # dry run, every source
    python -m expire --apply          # write
    python -m expire --source immowelt --max-age-days 21 --apply

Prerequisite: the weekly sweep rotation (`--sweep`, one category per day) must have
completed at least once. The incremental finder stops after a few pages and never
reaches older listings, so without the sweep `last_seen_at` is stale for live
listings too and this job would mark half the inventory offline. Two guards:

  * MAX_AGE_DAYS is comfortably longer than one rotation, so a single missed sweep
    costs nothing.
  * MAX_EXPIRY_SHARE refuses the run outright if an implausible share of a source
    is stale, which is what a blocked or half-finished rotation looks like.
"""

import argparse
import sys

from lib.database import Database
from lib.logger import get_logger
from lib.models import ListingSource

# Longer than one full sweep rotation (one category per day, all categories inside
# a week), so a listing has to miss roughly three consecutive sweeps before it is
# expired. That is the slack that lets this run unattended.
MAX_AGE_DAYS = 21

# A healthy run expires a trickle. Anything above this share of a source's active
# listings means the finder did not actually see the inventory — a blocked sweep,
# a changed result layout, a rotation that never ran — and expiring on that data
# would take the live inventory down with it. Refuse and exit red instead.
MAX_EXPIRY_SHARE = 0.25

logger = get_logger("expire")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source",
        action="append",
        choices=[s.value for s in ListingSource],
        help="Limit to this source (repeatable). Default: all.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=MAX_AGE_DAYS,
        help=f"Expire listings not seen in this many days (default: {MAX_AGE_DAYS}).",
    )
    parser.add_argument(
        "--max-share",
        type=float,
        default=MAX_EXPIRY_SHARE,
        help=f"Refuse if more than this share of a source is stale (default: {MAX_EXPIRY_SHARE}).",
    )
    parser.add_argument("--apply", action="store_true", help="Actually write. Without it this is a dry run.")
    args = parser.parse_args(argv)

    sources = [s for s in ListingSource if not args.source or s.value in args.source]
    db = Database()
    refused: list[str] = []
    total = 0

    for source in sources:
        stale, active = db.count_stale_listings(source, args.max_age_days)
        if active == 0:
            logger.info(f"{source.value}: nothing active, skipping")
            continue

        share = stale / active
        logger.info(
            f"{source.value}: {stale} of {active} active listings not seen in "
            f"{args.max_age_days} days ({share:.1%})"
        )

        if share > args.max_share:
            logger.error(
                f"{source.value}: REFUSING - {share:.1%} exceeds the {args.max_share:.0%} ceiling. "
                f"That is what a blocked or unfinished sweep looks like, not what a week of "
                f"listings going offline looks like. Confirm the sweep rotation actually "
                f"completed, then override once with --max-share 1.0 if the backlog is real."
            )
            refused.append(source.value)
            continue

        if not args.apply:
            logger.info(f"{source.value}: dry run, would expire {stale}. Pass --apply to write.")
            continue

        total += db.expire_stale_listings(source, args.max_age_days)

    if refused:
        logger.error(f"Refused {len(refused)} source(s): {', '.join(refused)}")
        return 1

    logger.info(f"Done. {total} listings expired." if args.apply else "Dry run complete, nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
