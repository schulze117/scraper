import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag
from lzstring import LZString

from lib.logger import get_logger
from lib.config import get_config, resolve_proxy
from lib.exceptions import ElementNotFoundError, NotBeautifulSoupError, StructureChangedError
from lib.models import IMMOWELT_SEARCH_CATEGORIES, ListingSource, NewListing
from .base import BaseFinder, run_finder

config = get_config()
logger = get_logger("immowelt")

lz = LZString()

# Everything extract_listing_data needs from an entry's `metadata`. An entry
# missing one of them is skipped and counted, not fatal -- one crooked object
# must not cost the page.
REQUIRED_METADATA_KEYS = ("id", "updateDate", "creationDate")

def has_listing_metadata(entry: Any) -> bool:
    metadata = entry.get("metadata") if isinstance(entry, dict) else None
    return isinstance(metadata, dict) and all(metadata.get(k) for k in REQUIRED_METADATA_KEYS)


class ImmoweltFinder(BaseFinder):
    SOURCE = ListingSource.IMMOWELT
    CONCURRENT_LOCATIONS = False
    BASE_URL = "https://www.immowelt.de/classified-search"
    # build_url pins order=DateDesc, so results are newest-first: >95% of new
    # listings land on pages 1-4 and the rest of the ~70 pages are already known.
    # Crawling to the bottom anyway is what broke this finder — DataDome starts
    # blocking around page 45, and the ~50 min of blocked fetches that follow burn
    # the IP badly enough that the next categories lose page 1 and are skipped
    # whole. Stop after 3 consecutive pages with nothing new; MAX_PAGES is the
    # safety net for the first run after an outage, when depth is actually useful.
    STOP_WHEN_NO_NEW = True
    NO_NEW_PAGES_TO_STOP = 3
    MAX_PAGES = 20
    # A results page holds 30 entries, 40 for HAUS_KAUFEN (measured 2026-09-24).
    # A page this full cannot be a one-page result set -- see get_pages_count.
    # The smaller of the two, so the check covers every category.
    FULL_PAGE_ENTRIES = 30

    def __init__(self):
        method = config.find.immowelt.method
        super().__init__(method=method, proxy_url=resolve_proxy("find", "immowelt"))

    def get_categories(self):
        return IMMOWELT_SEARCH_CATEGORIES.items()

    def get_locations(self):
        return self.config.finder.locations.immowelt

    def build_url(self, category: str, location: str, page: int = 0) -> str:
        url = f"{self.BASE_URL}?{category}&locations={location}&order=DateDesc"
        if page > 1:
            url += f"&page={page}"
        return url


    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        script_tag = soup.find("script", string=lambda text: text is not None and "__UFRN_FETCHER__" in text)  # type: ignore
        if not script_tag:
            # log the first 1000 characters of the page HTML for debugging
            logger.info("Script tag with __UFRN_FETCHER__ not found. Page HTML (first 1000 chars):\n" + soup.prettify()[:1000])
            raise ElementNotFoundError("Script tag with __UFRN_FETCHER__")
        if type(script_tag) != Tag:
            raise NotBeautifulSoupError("script_tag")
        if "classified-serp-init-data" not in str(script_tag):
            raise ValueError(f"classified-serp-init-data not found in script tag: {script_tag}")
        encoded = str(script_tag).split(r"\"classified-serp-init-data\":\"")[1].split('"}')[0]
        decoded = lz.decompressFromBase64(encoded)
        if not decoded:
            raise ValueError("Failed to decode JSON data from the script tag.")
        return json.loads(decoded)

    def get_result_entries(self, soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
        """The page's `classifiedsData`, keyed by listing id.

        Read strictly. The `.get()` chain this replaced turned a renamed key into
        `{}`, which scores as "no new listings", feeds the early stop and exits
        green.

        Empty is still a real answer: a search without hits carries
        `classifiedsData: {}`. It is believed only while `classifieds`, the
        page's own list of ids, is empty too -- ids without data means the data
        moved. The two do not match one to one: the last page of HAUS_KAUFEN
        listed 17 ids for 16 entries on 2026-09-24. So a last page holding
        nothing but such an orphan trips this falsely, which costs one sweep its
        `complete`; the silent version costs the whole crawl.
        """
        page_props = self.get_json_data(soup).get("pageProps")
        if not isinstance(page_props, dict):
            raise StructureChangedError("pageProps", "missing from classified-serp-init-data")
        entries = page_props.get("classifiedsData")
        if not isinstance(entries, dict):
            raise StructureChangedError("pageProps.classifiedsData", f"missing or not a dict ({type(entries).__name__})")
        if not entries and page_props.get("classifieds"):
            raise StructureChangedError(
                "pageProps.classifiedsData",
                f"empty, but `classifieds` lists {len(page_props['classifieds'])} ids for this page",
            )
        return entries

    def get_listings(self, soup: BeautifulSoup) -> list[NewListing]:
        result_entries = self.get_result_entries(soup)
        if not result_entries:
            self.logger.warning("No listings found on this page, skipping")
            return []

        listings: list[NewListing] = []
        skipped = 0
        for entry in result_entries.values():
            if not has_listing_metadata(entry):
                skipped += 1
                continue
            listings.append(extract_listing_data(entry["metadata"]))

        # Skipping a stray entry is routine; skipping every one of them is a
        # renamed field, and it must not pass as "no new listings".
        if not listings:
            raise StructureChangedError(
                "classifiedsData",
                f"{len(result_entries)} entries on the page, none with metadata {list(REQUIRED_METADATA_KEYS)}",
            )
        if skipped:
            self.logger.warning(
                f"Skipped {skipped} unparsable entries of {len(result_entries)} on the page "
                f"(metadata without {list(REQUIRED_METADATA_KEYS)}) — a rising share is the "
                f"warning before it reaches all of them."
            )
        return listings

    def get_pages_count(self, soup: BeautifulSoup) -> int:
        pagination_buttons_container = soup.find("nav", attrs={"data-testid": "serp-pagination-testid"})
        if not pagination_buttons_container:
            raise ElementNotFoundError("Pagination buttons container")
        if type(pagination_buttons_container) != Tag:
            raise NotBeautifulSoupError("pagination_buttons_container")
        pagination_buttons = pagination_buttons_container.find_all("button")
        if len(pagination_buttons) >= 2:
            return int(pagination_buttons[-2].get_text(strip=True))

        # The nav is there but holds no page buttons: either the result set fits
        # on one page (a single-page search renders the nav empty), or the
        # buttons became something else and every page behind page 1 is about to
        # go unvisited while the run stays green. A full page cannot be the
        # first case. Same trade as immoscout: a category with exactly one full
        # page of hits trips this falsely once.
        entry_count = len(self.get_result_entries(soup))
        if entry_count >= self.FULL_PAGE_ENTRIES:
            raise StructureChangedError(
                "serp-pagination-testid",
                f"{entry_count} entries on the page but fewer than two pagination buttons",
            )

        self.logger.info(f"No pagination buttons, {entry_count} entries — single-page result set.")
        return 1


def extract_listing_data(listing: dict[str, str]) -> NewListing:
    external_id = listing.get("id")
    modified_at = listing.get("updateDate")
    created_at = listing.get("creationDate")
    if not external_id:
        raise ValueError(f"id not found in listing data: {listing}")
    if not modified_at:
        raise ValueError(f"updateDate not found in listing data: {listing}")
    if not created_at:
        raise ValueError(f"creationDate not found in listing data: {listing}")
    return NewListing(
        external_id=external_id,
        created_at=datetime.fromisoformat(created_at),
        modified_at=datetime.fromisoformat(modified_at),
        source=ListingSource.IMMOWELT,
    )


if __name__ == "__main__":
    run_finder(ImmoweltFinder)
