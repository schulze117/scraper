import concurrent.futures
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from bs4 import BeautifulSoup

from lib.config import env_get, get_config
from lib.database import Database
from lib.exceptions import BotDetectedError, FetchNetworkError, InactiveListingError
from lib.fetch.fetcher import Fetcher
from lib.logger import get_logger
from lib.models import ListingSource, NextListingModel


class BaseScraper(ABC):
    # Default behavior: process listings concurrently (curl_cffi supports parallelism)
    CONCURRENT_LISTINGS = True
    # When True: only re-scrape if modified_at > last_scraped_at (Immowelt, Immoscout)
    # When False: re-scrape any listing older than 12h (Kleinanzeigen — no modified_at updates)
    RESCRAPE_ON_MODIFIED_ONLY = False
    # A string only the fully-rendered page contains; the browser fetcher waits
    # for it so we don't capture the pre-hydration shell. None = no wait (curl).
    READY_MARKER: str | None = None
    # Wall-clock minutes one run may use before it stops itself.
    #
    # This default is only the fallback for a hand-run dispatch. In production the
    # scheduler passes the number in as `SCRAPE_TIME_BUDGET_MIN`, because a budget
    # has to fit the gap to the next fire and the cron line is what decides that
    # gap — so both live together in /etc/cron.d/fixfolio on the VPS, and neither
    # is in this file. See ../ecosystem.md. The three platforms deliberately do
    # not share a number: immoscout is arrival-limited (100 min every 2 h),
    # kleinanzeigen is throughput-limited (320 min every 6 h).
    #
    # The ceiling that does not move is the runner. A GitHub-hosted job is killed
    # at 6 h and that kill is reported as `cancelled` — no exit code, no summary
    # line, indistinguishable from a crash. That is how three scrape workflows
    # spent August looking broken while working correctly.
    #
    # Overrunning is not corruption — batches are claimed with FOR UPDATE SKIP
    # LOCKED, so two runs never take the same listing — but it doubles the load
    # on one portal and one residential proxy, which is what the `concurrency`
    # groups exist to prevent.
    #
    # Stopping costs nothing: the queue is claimed in batches and every listing
    # commits its own row, so whatever is left stays queued for the next run.
    # `0` means unlimited, which is what a local drain wants.
    TIME_BUDGET_MIN = 270

    # Consecutive blocked fetches that end the run with exit 42, which the
    # workflow answers by re-dispatching onto a fresh runner IP (up to 15 times).
    #
    # The number has to separate two things that look identical for one listing:
    # a portal having a bad moment with us, and the portal having decided about
    # this IP. One block is noise -- the listing is skipped, stays queued, and
    # the next one usually succeeds. Several in a row is a wall, and every fetch
    # after it is wasted: on 2026-09-17 immowelt blocked 19 in a row over 40
    # minutes and the run would have kept going for another four hours.
    #
    # Deliberately low. The cost of stopping early is one re-dispatch; the cost
    # of continuing is a queue drained against a wall.
    BOT_BLOCKS_TO_ABORT = 4

    # Consecutive listings the browser could not reach at all (FetchNetworkError:
    # a dead proxy tunnel, DNS, no route) that end the run with exit 1. Not 42:
    # a new runner does not bring a proxy back. Each one has already been retried
    # inside the fetch, so five in a row is ~10 minutes of nothing. On 2026-09-23,
    # with the AI server that hosts the proxy powered off, immoscout ran its full
    # budget twice, got 0 of 82 and ended green.
    NETWORK_ERRORS_TO_ABORT = 5

    # Consecutive listings that were fetched but failed to parse, ending the run
    # with exit 1. One is a broken ad; a streak is the portal having changed its
    # page. On 2026-09-23 immowelt renamed the key its page state sits under,
    # and every run after failed every listing and ended green. In normal runs
    # the longest streak is 0 (kleinanzeigen, 4 851 listings, 2026-09-24).
    PARSE_FAILURES_TO_ABORT = 10

    def __init__(self, source: ListingSource, method: str, proxy_url: str | None):
        self.source = source
        self.config = get_config()
        self.logger = get_logger(self.__class__.__name__)
        self.db = Database()
        self.fetcher = Fetcher(method=method, proxy_url=proxy_url)
        method_config = getattr(self.config, method)
        self.max_workers = method_config.max_workers
        # Consecutive blocks, reset by any successful fetch. Touched from several
        # threads when CONCURRENT_LISTINGS is on, so it takes the lock.
        self._consecutive_blocks = 0
        self._consecutive_unreachable = 0
        self._consecutive_parse_failures = 0
        self._block_lock = threading.Lock()

    def _time_budget_s(self) -> float:
        """Seconds of wall clock this run may use; 0 for unlimited.

        `SCRAPE_TIME_BUDGET_MIN` wins over the class default, so the number can
        be retuned from the workflow — where it has to stay under that job's own
        `timeout-minutes` — without shipping code.
        """
        minutes = self.TIME_BUDGET_MIN
        raw = env_get("SCRAPE_TIME_BUDGET_MIN")
        if raw is not None:
            try:
                minutes = float(raw)
            except ValueError:
                self.logger.warning(
                    f"SCRAPE_TIME_BUDGET_MIN={raw!r} is not a number; using {minutes} min."
                )
        return max(0.0, minutes) * 60.0

    def run(self):
        """
        Main strategy:
        1. Fetch a batch of unscraped listings from the DB
        2. Process each listing (Concurrently OR Sequentially based on flag)
        3. Repeat until the queue is empty, the time budget is spent, or Ctrl+C
        """
        scrape_config = getattr(self.config.scrape, self.source.value)
        batch_size = scrape_config.batch_size
        total_scraped = 0
        budget_s = self._time_budget_s()
        started = time.monotonic()

        def over_budget() -> bool:
            return bool(budget_s) and (time.monotonic() - started) >= budget_s

        try:
            while True:
                if over_budget():
                    break

                listings = self.db.get_next_listings(
                    self.source, batch_size, rescrape_on_modified_only=self.RESCRAPE_ON_MODIFIED_ONLY
                )

                if not listings:
                    self.logger.info(f"No more listings to scrape. Total processed: {total_scraped}")
                    return

                self.logger.info(
                    f"Starting batch of {len(listings)} listings. "
                    f"Concurrency: {'ON' if self.CONCURRENT_LISTINGS else 'OFF'}."
                )

                if self.CONCURRENT_LISTINGS:
                    # A concurrent batch always runs to completion: its listings
                    # are already claimed, and cancelling futures mid-flight would
                    # leave them claimed but unscraped until the claim expires.
                    # curl_cffi batches are short, so between batches is a fine
                    # place to be the only checkpoint.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                        futures = [
                            executor.submit(self.process_listing, listing)
                            for listing in listings
                        ]
                        concurrent.futures.wait(futures)
                    total_scraped += len(listings)
                else:
                    # Checked per listing rather than per batch: one seleniumbase
                    # fetch can burn FETCH_HARD_TIMEOUT x max_retries (~6 min), so
                    # a batch boundary is too coarse to keep the overshoot inside
                    # the headroom the default budget leaves.
                    for listing in listings:
                        self.process_listing(listing)
                        total_scraped += 1
                        if over_budget():
                            break

        except KeyboardInterrupt:
            self.logger.info(f"Interrupted. Total processed: {total_scraped}")
            return

        self.logger.info(
            f"Time budget of {budget_s / 60:.0f} min spent after "
            f"{(time.monotonic() - started) / 60:.0f} min. Stopping cleanly; the rest "
            f"stays queued for the next run. Total processed: {total_scraped}"
        )

    def process_listing(self, listing: NextListingModel):
        """Fetch, extract, and save data for a single listing."""
        url = self.build_url(listing.external_id)
        prefix = f"{listing.id}  {url}"
        fetched = False
        try:
            html = self.fetcher.fetch(url, ready_marker=self.READY_MARKER)
            fetched = True
            # Reset here, not after a successful scrape: the counter measures
            # blocked fetches, and a page that came back but parsed as a deleted
            # ad is still proof that the portal is talking to us.
            self._note_fetch_ok()
            soup = BeautifulSoup(html, "lxml")

            minified_html = self.get_minified_html(soup)
            json_data = self.get_json_data(soup)
            image_urls = self.get_image_urls(soup, json_data)
            main_image_url = self.get_main_image_url(soup, json_data)
            extra_data = self.get_extra_data(soup, json_data)

            self.db.set_raw_data(listing.id, minified_html, json_data)
            self.db.set_image_urls(listing.id, image_urls)
            if main_image_url:
                self.db.set_main_image_url(listing.id, main_image_url)
            if extra_data:
                self.db.update_extra_data(listing.id, extra_data)
            self.db.set_last_scraped(listing.id)

            self.logger.info(f"{prefix}  Scraped successfully")
            self._note_parsed()

        except BotDetectedError as e:
            # Never a deactivation: we did not see the page, so we know nothing
            # about whether the ad still exists. The listing stays queued and the
            # next run picks it up.
            self.logger.warning(f"{prefix}  Blocked, skipping (not deactivating): {e}")
            self._note_block()

        except FetchNetworkError as e:
            # Nor this: we never reached the portal.
            self.logger.error(f"{prefix}  Unreachable, skipping: {e}")
            self._note_unreachable()

        except InactiveListingError:
            self._note_parsed()
            if listing.last_scraped_at is None:
                self.logger.info(f"{prefix}  Never scraped, deleting (inactive listing)")
                self.db.delete_listing(listing.id)
            else:
                self.logger.info(f"{prefix}  Deactivating (inactive listing)")
                self.db.deactivate_listing(listing.id)

        except Exception as e:
            if self.is_deactivated_listing(e, listing):
                if listing.last_scraped_at is None:
                    self.logger.info(f"{prefix}  Never scraped, deleting: {e}")
                    self.db.delete_listing(listing.id)
                else:
                    self.logger.info(f"{prefix}  Deactivating: {e}")
                    self.db.deactivate_listing(listing.id)
            else:
                self.logger.error(f"{prefix}  Failed to scrape: {e}")
                if fetched:
                    self._note_parse_failure()

    def _note_fetch_ok(self) -> None:
        """A page came back. Whatever wall we were seeing is not there now."""
        with self._block_lock:
            self._consecutive_blocks = 0
            self._consecutive_unreachable = 0

    def _note_block(self) -> None:
        """Count a block and end the run once they stop looking like noise.

        os._exit rather than an exception: this can run inside a worker thread,
        where raising only kills that future and leaves the rest of the batch
        fetching into the same wall. 42 is the code the workflow watches for --
        it re-dispatches on a fresh IP instead of reporting a failure.
        """
        with self._block_lock:
            self._consecutive_blocks += 1
            n = self._consecutive_blocks
        if n < self.BOT_BLOCKS_TO_ABORT:
            self.logger.warning(f"Blocked fetch {n}/{self.BOT_BLOCKS_TO_ABORT} in a row.")
            return
        self.logger.error(
            f"{n} blocked fetches in a row — this IP is walled. Stopping with exit 42 "
            f"so the workflow retries on a fresh runner. Unscraped listings stay queued."
        )
        os._exit(42)

    def _note_parsed(self) -> None:
        """A fetched page was understood, as a listing or as a deleted one."""
        with self._block_lock:
            self._consecutive_parse_failures = 0

    def _note_parse_failure(self) -> None:
        """Count a page that came back but did not parse; end the run on a streak.

        os._exit for the same reason as `_note_block`.
        """
        with self._block_lock:
            self._consecutive_parse_failures += 1
            n = self._consecutive_parse_failures
        if n < self.PARSE_FAILURES_TO_ABORT:
            return
        self.logger.error(
            f"{n} listings in a row failed to parse — the portal has likely changed "
            f"its page. Stopping with exit 1. Unscraped listings stay queued."
        )
        os._exit(1)

    def _note_unreachable(self) -> None:
        """Count a listing the browser never reached; end the run on a streak.

        os._exit for the same reason as `_note_block`.
        """
        with self._block_lock:
            self._consecutive_unreachable += 1
            n = self._consecutive_unreachable
        if n < self.NETWORK_ERRORS_TO_ABORT:
            return
        self.logger.error(
            f"{n} listings in a row unreachable — the proxy or the network is down. "
            f"Stopping with exit 1. Unscraped listings stay queued."
        )
        os._exit(1)

    # --- Abstract Methods ---

    @abstractmethod
    def build_url(self, external_id: str) -> str:
        pass

    @abstractmethod
    def get_minified_html(self, soup: BeautifulSoup) -> str:
        pass

    @abstractmethod
    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_image_urls(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> list[str]:
        pass

    @abstractmethod
    def get_main_image_url(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> str | None:
        pass

    # --- Optional Overrides ---

    def get_extra_data(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """
        Extracts extra data to be persisted.
        Returns a dict with table names as keys and column dicts as values.
        """
        return {}

    def is_deactivated_listing(self, exception: Exception, listing: NextListingModel) -> bool:
        """
        Returns True if the exception indicates the listing has been deactivated/removed.
        Override in subclasses to implement platform-specific detection.
        """
        return False