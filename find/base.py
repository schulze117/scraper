import argparse
import concurrent.futures
import sys
import threading
from abc import ABC, abstractmethod
from bs4 import BeautifulSoup
from lib.logger import get_logger
from lib.fetch.fetcher import Fetcher
from lib.database import Database
from lib.config import get_config
from lib.models import ListingSource
from lib.exceptions import FinderFailedError, ResultTailReachedError


class BaseFinder(ABC):
    # Which portal this finder speaks for. Only the sweep record needs it -- the
    # listings themselves carry their own source from get_listings.
    SOURCE: ListingSource
    # Default behavior: Process locations sequentially (safer for tough sites like Immoscout)
    CONCURRENT_LOCATIONS = False
    CONCURRENT_PAGES = True
    # A string only the fully-rendered page contains; the browser fetcher waits
    # for it so we don't capture the pre-hydration shell. None = no wait (curl).
    READY_MARKER: str | None = None
    # Results are newest-first, so once a page has zero new listings the deeper
    # pages are all already known. When True, paginate sequentially and stop
    # there (Immoscout: 453 pages/category at ~30s each is otherwise hours).
    STOP_WHEN_NO_NEW: bool = False
    # How many *consecutive* pages must yield zero new listings before stopping.
    # >1 guards against a single page that happens to be all-known (promoted /
    # sponsored listings are pinned to page 1, so it can look empty while the
    # pages behind it are full of new ones).
    NO_NEW_PAGES_TO_STOP: int = 3
    # Hard safety cap on pages per location, whatever STOP_WHEN_NO_NEW decides.
    MAX_PAGES: int | None = None
    # Consecutive failed pages that end a sweep. Only used in sweep mode, where
    # the crawl goes deep enough to hit a bot wall mid-run; the incremental finder
    # never gets far enough for this to matter.
    SWEEP_FAILURES_TO_ABORT: int = 10
    # Consecutive all-undated pages that mean the result set has ended, and the
    # earliest point in a category at which that may be believed, as a share of
    # the advertised page count.
    #
    # Both halves guard the same mistake. A parser signals the tail by raising
    # ResultTailReachedError (immoscout: every entry intact but carrying no
    # `@creation`), and the honest reading of that is "the dated listings ran
    # out". The dishonest one is "`@creation` was renamed and every page now
    # looks like this" — which would end each category on page 1 and record the
    # sweep as COMPLETE, handing reconcile.py permission to deactivate an entire
    # portal. So the run wants to see it three pages running, and not before it
    # is a tenth of the way in. A rename fails both tests on page 1; a real tail
    # sits at 40-95 % of the advertised count and passes them easily.
    RESULT_TAIL_PAGES_TO_STOP: int = 3
    RESULT_TAIL_MIN_DEPTH_SHARE: float = 0.10
    # Page 1 decides the fate of the whole category — it is the only page that
    # yields the page count, so losing it discards every page behind it. One
    # blocked fetch must not cost a category, so it gets its own retries.
    PAGE_ONE_ATTEMPTS: int = 3

    def __init__(self, method: str, proxy_url: str | None):
        self.config = get_config()
        self.logger = get_logger(self.__class__.__name__)
        # Set by run_finder from the command line. `only_categories` restricts the
        # run to one category (the sweep rotation crawls one per day); `sweep`
        # turns off both depth limits so the run reaches the last page, which is
        # what refreshes `last_seen_at` for listings too old to sit on page 1.
        self.only_categories: set[str] | None = None
        self.sweep: bool = False
        # Sweep completeness, tallied across the whole run. The bar is zero
        # failed pages, because reconcile.py reads `complete` as permission to
        # deactivate everything the run did not see.
        #
        # `_hard_incomplete` is what a retry cannot rescue: a lost page 1, or a
        # pagination we abandoned mid-way. `_failed_pages` is what it can --
        # individual pages that errored, retried once before the run is scored.
        self._pages_ok: int = 0
        self._pages_failed: int = 0
        self._hard_incomplete: bool = False
        self._failed_pages: list[tuple] = []
        self._sweep_detail: list[str] = []
        # Pages that were the undated tail rather than listings. Recorded so the
        # sweep row shows how much of the advertised depth was real.
        self._pages_tail: int = 0
        # Kleinanzeigen crawls locations and pages concurrently, so the tallies
        # are touched from several threads. `complete` is a plain assignment and
        # safe either way; the counts are not, and they end up in the record.
        self._tally_lock = threading.Lock()
        self.db = Database()
        self.fetcher = Fetcher(method=method, proxy_url=proxy_url)
        # get worker based on method and config 
        # get max_workers based on method and config
        method_config = getattr(self.config, method)
        self.max_workers = method_config.max_workers
        
    def fetch_html(self, url: str) -> str:
        return self.fetcher.fetch(url, ready_marker=self.READY_MARKER)

    @property
    def is_exhaustive(self) -> bool:
        """Does this run walk every page, so its absences mean something?

        True in sweep mode, and also for a finder that has no early stop at all —
        kleinanzeigen crawls to the last page on every ordinary run, so every one
        of its runs is a sweep and gets recorded as one. Without this it would
        never produce a complete sweep record and reconcile.py would block on it
        forever, which is the correct default for a source nothing sweeps and the
        wrong answer for this one.
        """
        return self.sweep or not self.STOP_WHEN_NO_NEW

    def select_categories(self) -> list[tuple]:
        """The categories this run should crawl.

        Without --category that is all of them, exactly as before. With it, the
        run is restricted to the named ones — that is how the weekly sweep spreads
        the deep crawl over several days without a tracking table: each day takes
        one category, and after a full rotation every live listing has been seen
        again.

        An unknown name is fatal rather than empty. A typo in the cron line would
        otherwise crawl nothing and exit green, which reconcile.py would read as a
        category nobody sweeps — blocking deactivation for the whole source.
        """
        categories = list(self.get_categories())
        if not self.only_categories:
            return categories

        available = {name.value: name for name, _ in categories}
        unknown = self.only_categories - available.keys()
        if unknown:
            raise ValueError(
                f"Unknown categor{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(sorted(unknown))} for {self.__class__.__name__}. "
                f"Available: {', '.join(sorted(available))}"
            )

        selected = [(name, category) for name, category in categories if name.value in self.only_categories]
        self.logger.info(
            f"Restricted to {len(selected)} of {len(categories)} categories: "
            f"{', '.join(sorted(self.only_categories))}"
        )
        return selected

    def run(self):
        """
        Main strategy:
        1. Iterate Categories
        2. Iterate Locations (Concurrently OR Sequentially based on flag)
        3. Iterate Pages (Concurrent by default for speed)

        Raises FinderFailedError if any (category, location) lost page 1, which
        means its whole result set was silently skipped. Losing page 1 used to
        end the category with a clean exit code, so a total outage looked
        identical to a quiet day — see the immoscout blackout of 2026-08-16.
        """
        lost: list[str] = []
        total_new = 0

        categories = self.select_categories()
        run_id = None
        if self.is_exhaustive:
            run_id = self.db.start_sweep_run(
                self.SOURCE, sorted(name.value for name, _ in categories))

        try:
            total_new, lost = self._crawl(categories)
        finally:
            if run_id is not None:
                if lost:
                    self._hard_incomplete = True
                    self._sweep_detail.append(f"lost page 1: {', '.join(lost)}")
                self._retry_failed_pages()
                complete = not self._hard_incomplete and not self._failed_pages
                if self._failed_pages:
                    self._sweep_detail.append(
                        f"{len(self._failed_pages)} pages still failing after retry")
                if self._pages_tail:
                    self._sweep_detail.append(f"{self._pages_tail} undated tail pages")
                self.db.finish_sweep_run(
                    run_id, complete, self._pages_ok, self._pages_failed,
                    "; ".join(self._sweep_detail) or None)

        self.logger.info(f"Crawl finished: {total_new} new listings, {len(lost)} lost page 1.")
        if lost:
            raise FinderFailedError(lost, total_new)

    def _retry_failed_pages(self) -> None:
        """One more attempt at the pages that errored, before writing the run off.

        Zero failed pages is the right bar — a page we could not read is a page
        whose listings we cannot claim to have seen — but on a big crawl it is a
        bar nothing clears by luck. Kleinanzeigen's find walks ~2 730 pages and
        lost 10 of them to transient errors on 2026-09-13; under a strict rule
        with no retry that would have blocked reconciliation for the source
        forever while the crawl was in fact 99.6 % fine.

        Retrying is what makes the strict bar affordable: a transient failure
        costs one more fetch, and only a page that fails twice blocks the source.

        Sequential whatever CONCURRENT_PAGES says. This is a short tail, and if
        the failures were rate-limiting then going again in parallel is the one
        thing guaranteed not to help.
        """
        pending, self._failed_pages = self._failed_pages, []
        if not pending:
            return

        # Take the first attempt's failures back off the tally and let the retry
        # score these pages from scratch: process_page_strategy counts every
        # failure it sees, so leaving them on would count a page that fails twice
        # as two failed pages. That is how a 10-page abort was recorded as 20.
        with self._tally_lock:
            self._pages_failed -= len(pending)

        self.logger.info(f"Retrying {len(pending)} failed page(s) before scoring the run.")
        recovered = 0
        for category, location, page in pending:
            _, new_count, _ = self.process_page_strategy(category, location, page)
            if new_count is not None:
                recovered += 1
        self.logger.info(
            f"Retry recovered {recovered} of {len(pending)} pages; "
            f"{len(self._failed_pages)} still failing."
        )

    def _crawl(self, categories) -> tuple[int, list[str]]:
        """The crawl itself. Split out of run() so the sweep record is written
        even when this raises."""
        lost: list[str] = []
        total_new = 0

        for category_name, category in categories:
            locations = self.get_locations()

            self.logger.info(
                f"Starting crawl for {category_name} with {len(locations)} locations. "
                f"Concurrency for locations: {'ON' if self.CONCURRENT_LOCATIONS else 'OFF'}. "
                f"Concurrency for pages: {'ON' if self.CONCURRENT_PAGES else 'OFF'}."
            )

            if self.CONCURRENT_LOCATIONS:
                # Parallel processing for sites that allow it (e.g. Kleinanzeigen)
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                    futures = {
                        executor.submit(self.process_location, category, location): location
                        for location in locations
                    }
                    for future in concurrent.futures.as_completed(futures):
                        page_one_ok, new_count = future.result()
                        total_new += new_count
                        if not page_one_ok:
                            lost.append(f"{category_name}/{futures[future]}")
            else:
                # Sequential processing for sensitive sites (e.g. Immoscout or Immowelt)
                for location in locations:
                    page_one_ok, new_count = self.process_location(category, location)
                    total_new += new_count
                    if not page_one_ok:
                        lost.append(f"{category_name}/{location}")

        return total_new, lost

    def process_location(self, category, location) -> tuple[bool, int]:
        """Strategy for a single location.

        Returns (page 1 succeeded, new listings saved for this location).
        """
        # 1. Process Page 1 and get total page count + how many were new. Page 1
        # is retried on its own: it carries the page count, so a single blocked
        # fetch would otherwise discard every page behind it.
        for attempt in range(1, self.PAGE_ONE_ATTEMPTS + 1):
            pages_count, new_count, _ = self.process_page_strategy(category, location, page=1)
            if new_count is not None:
                break
            if attempt < self.PAGE_ONE_ATTEMPTS:
                self.logger.warning(
                    f"Page 1 failed for {location} "
                    f"(attempt {attempt}/{self.PAGE_ONE_ATTEMPTS}); retrying."
                )
        else:
            self.logger.error(
                f"Page 1 failed for {location} after {self.PAGE_ONE_ATTEMPTS} attempts — "
                f"skipping the whole category, no listings collected."
            )
            return False, 0

        location_new = new_count

        last_page = pages_count
        if self.MAX_PAGES:
            last_page = min(last_page, self.MAX_PAGES)
        if last_page <= 1:
            return True, location_new

        # 2a. Sweep mode: every page, in order, no early stop. Sequential on
        # purpose — CONCURRENT_PAGES is what the default branch below uses, and
        # firing max_workers deep pages at immowelt or immoscout is the fastest
        # way to get the run blocked. Depth is already the risk here; concurrency
        # on top of it is not a trade worth making for a weekly job.
        if self.sweep:
            consecutive_failures = 0
            tail_streak = 0
            # The earliest page at which an all-undated run may be read as the end
            # of the results. A real tail begins around 40-95 % of the advertised
            # count; anything in the first tenth is far likelier to be `@creation`
            # having gone missing everywhere, and calling that "complete" would
            # licence reconcile.py to deactivate the portal.
            tail_floor = max(self.RESULT_TAIL_PAGES_TO_STOP,
                             int(last_page * self.RESULT_TAIL_MIN_DEPTH_SHARE))
            dated_pages = 0
            for page in range(2, last_page + 1):
                _, new_count, is_tail = self.process_page_strategy(category, location, page)

                if is_tail:
                    # Undated entries sort behind every dated one, so this is the
                    # portal running out of listings rather than a bad page. It
                    # ends the category successfully: the pages behind it hold
                    # nothing we store, so never visiting them costs no
                    # `last_seen_at` and must not cost the sweep its `complete`.
                    tail_streak += 1
                    with self._tally_lock:
                        self._pages_tail += 1
                    # Three conditions, and the third is the one that is not
                    # obvious: we must have *harvested* at least as many dated
                    # pages as the floor demands. Depth alone can be reached by
                    # skipping — a run where `@creation` vanished from page 2
                    # onwards would arrive at the floor having stored one page and
                    # then declare the category fully swept. Requiring real dated
                    # pages means the shortcut is only ever taken by a crawl that
                    # actually found an inventory to shorten.
                    if (tail_streak >= self.RESULT_TAIL_PAGES_TO_STOP
                            and page >= tail_floor
                            and dated_pages >= tail_floor):
                        self.logger.info(
                            f"Result tail reached at page {page} of {last_page} for "
                            f"{location}: {tail_streak} consecutive pages of intact but "
                            f"undated entries, past the {tail_floor}-page floor. The "
                            f"dated inventory ends here; category counts as fully swept."
                        )
                        self._sweep_detail.append(
                            f"tail at page {page}/{last_page} for {location}")
                        break
                    continue
                tail_streak = 0

                if new_count is None:
                    # Deep pages are where the bot walls live: DataDome starts
                    # blocking immowelt around page 45. A run streak of failures
                    # means we are blocked, not that those pages are empty —
                    # grinding through the remaining hundred proves nothing and
                    # just burns runner minutes against a wall.
                    consecutive_failures += 1
                    if consecutive_failures >= self.SWEEP_FAILURES_TO_ABORT:
                        self.logger.error(
                            f"Sweep aborted at page {page} of {last_page} for {location}: "
                            f"{consecutive_failures} consecutive failed pages — treating this "
                            f"as blocked. Listings past here kept their old last_seen_at."
                        )
                        self._hard_incomplete = True
                        self._sweep_detail.append(
                            f"aborted at page {page}/{last_page} for {location}")
                        break
                    continue
                consecutive_failures = 0
                dated_pages += 1
                location_new += new_count
            return True, location_new

        # 2b. Early-stop mode: walk pages in order, stop once NO_NEW_PAGES_TO_STOP
        # consecutive pages have no new listings (deeper pages are older, so all
        # already known). A single new listing resets the streak. A failed page
        # (new_count is None) is neutral — it neither confirms nor breaks the
        # streak, so a fetch error can't be mistaken for "no new listings".
        if self.STOP_WHEN_NO_NEW:
            empty_streak = 1 if new_count == 0 else 0
            if empty_streak >= self.NO_NEW_PAGES_TO_STOP:
                return True, location_new
            for page in range(2, last_page + 1):
                _, new_count, _ = self.process_page_strategy(category, location, page)
                if new_count is None:
                    continue
                location_new += new_count
                empty_streak = empty_streak + 1 if new_count == 0 else 0
                if empty_streak >= self.NO_NEW_PAGES_TO_STOP:
                    self.logger.info(
                        f"Early stop at page {page} for {location}: "
                        f"{empty_streak} consecutive pages with no new listings."
                    )
                    break
            return True, location_new

        # 2c. Default: process the remaining pages concurrently
        if self.CONCURRENT_PAGES:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = [
                    executor.submit(self.process_page_strategy, category, location, page)
                    for page in range(2, last_page + 1)
                ]
                for future in concurrent.futures.as_completed(futures):
                    _, new_count, _ = future.result()
                    location_new += new_count or 0

        return True, location_new

    def process_page_strategy(self, category, location, page) -> tuple[int, int | None, bool]:
        """
        Builds URL, fetches HTML, parses listings, saves to DB.
        Returns (total pages count, number of NEW listings on this page, is_tail).
        new_count is None when the page yielded nothing usable — so early-stop
        won't mistake a failed fetch for "no new listings".

        `is_tail` says the page held only well-formed but undated entries, which
        on a newest-first crawl means the dated result set has ended. It is a
        *report*, not a verdict: the page still comes back as new_count=None, so
        anything that does not act on the flag keeps treating it as a bad page.
        Only the sweep's pagination acts on it, and only after seeing it three
        times running and deep enough into the category — see process_location.
        That default matters, because it is what still fails a category whose
        page 1 reads as tail, which is what a renamed `@creation` would look like.
        """
        url = self.build_url(category, location, page)

        try:
            # use the fetcher class to get the HTML.
            html = self.fetcher.fetch(url, ready_marker=self.READY_MARKER)
            soup = BeautifulSoup(html, "lxml")

            # Get listings and save
            listings = self.get_listings(soup)
            # This is also saving "alternative" listings. To avoide this dont save them if pages_count is 1
            new_count = 0
            if listings:
                new_count = self.db.set_new_listing_data(listings)

            # Get page count
            pages_count = self.get_pages_count(soup)

            self.logger.info(
                f"Listings: {len(listings):<3} (new: {new_count}) \tPage: {page} of {pages_count}"
                # f"\tCategory {category} \tLocation {location}"
                f"\tURL {url}"
            )
            with self._tally_lock:
                self._pages_ok += 1
            return pages_count, new_count, False

        except ResultTailReachedError as e:
            # Not a failure and not a listing page: the end of the dated results.
            # Deliberately left out of both tallies — process_location books it as
            # a tail page once it is sure, and as nothing at all if it is not.
            self.logger.info(f"Tail page {page} for {location}: {e}")
            return 0, None, True

        except Exception as e:
            self.logger.error(f"Failed page {page} for {location} (URL: {url}): {e}")
            # One failed page disqualifies the whole sweep. A page we could not
            # read is a page whose listings we cannot claim to have seen, and
            # reconcile.py would otherwise deactivate every one of them.
            with self._tally_lock:
                self._pages_failed += 1
                self._failed_pages.append((category, location, page))
            return 0, None, False

    # --- Abstract Methods ---

    @abstractmethod
    def get_categories(self) -> list[tuple]:
        pass

    @abstractmethod
    def get_locations(self) -> list[str]:
        pass

    @abstractmethod
    def build_url(self, category: str, location: str, page: int) -> str:
        pass

    @abstractmethod
    def get_listings(self, soup: BeautifulSoup) -> list:
        pass

    @abstractmethod
    def get_pages_count(self, soup: BeautifulSoup) -> int:
        pass


def run_finder(finder_cls: type[BaseFinder], argv: list[str] | None = None) -> None:
    """Entry point for every `python -m find.<platform>` module.

    Exists so a crawl that collected nothing turns the workflow run RED. The
    finders used to swallow every per-page error and return normally, so a
    totally blocked run exited 0 and looked identical to a quiet day — the
    immoscout blackout of August 2026 ran green for two days.

    Two modes:

      incremental (default)  what the frequent cron runs. Newest-first, stops
                             after NO_NEW_PAGES_TO_STOP empty pages, never goes
                             past MAX_PAGES. Finds new listings.
      --sweep                the weekly rotation. Crawls every page of the
                             selected categories, which refreshes `last_seen_at`
                             for the whole live inventory — the signal the
                             age-based expiry needs. Slow and far more likely to
                             be blocked, so it is a separate run and must never
                             replace the incremental one.
    """
    parser = argparse.ArgumentParser(description=finder_cls.__doc__ or finder_cls.__name__)
    parser.add_argument(
        "--category",
        action="append",
        metavar="NAME[,NAME...]",
        help="Crawl only these categories (repeatable, or comma-separated). "
             "Default: all of them.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Crawl to the last page: no early stop, no MAX_PAGES cap.",
    )
    args = parser.parse_args(argv)

    finder = finder_cls()
    if args.category:
        # Comma-separated as well as repeatable: a workflow_dispatch input is a
        # single string, and the sweep rotation runs two categories per slot.
        finder.only_categories = {
            c.strip().upper() for arg in args.category for c in arg.split(",") if c.strip()}
    if args.sweep:
        # Instance attributes shadow the class defaults for this run only.
        finder.sweep = True
        finder.MAX_PAGES = None
        finder.logger.info("Sweep mode: crawling every page (no early stop, no page cap).")

    try:
        finder.run()
    except ValueError as exc:
        finder.logger.error(str(exc))
        sys.exit(2)
    except FinderFailedError as exc:
        finder.logger.error(str(exc))
        sys.exit(1)