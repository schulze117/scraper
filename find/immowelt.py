import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag
from lzstring import LZString

from lib.logger import get_logger
from lib.config import get_config, resolve_proxy
from lib.exceptions import ElementNotFoundError, NotBeautifulSoupError
from lib.models import IMMOWELT_SEARCH_CATEGORIES, ListingSource, NewListing
from .base import BaseFinder, run_finder

config = get_config()
logger = get_logger("immowelt")

lz = LZString()


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

    def get_listings(self, soup: BeautifulSoup) -> list[NewListing]:
        json_data = self.get_json_data(soup)
        result_entries: dict[str, dict[str, Any]] = json_data.get("pageProps", {}).get("classifiedsData", {})
        listings: list[NewListing] = []
        for entry in result_entries.values():
            if "metadata" not in entry:
                continue
            listings.append(extract_listing_data(entry["metadata"]))
        return listings

    def get_pages_count(self, soup: BeautifulSoup) -> int:
        pagination_buttons_container = soup.find("nav", attrs={"data-testid": "serp-pagination-testid"})
        if not pagination_buttons_container:
            raise ElementNotFoundError("Pagination buttons container")
        if type(pagination_buttons_container) != Tag:
            raise NotBeautifulSoupError("pagination_buttons_container")
        pagination_buttons = pagination_buttons_container.find_all("button")
        if len(pagination_buttons) < 2:
            self.logger.warning("No pagination buttons found, assuming only one page")
            return 1
        last_page_number = int(pagination_buttons[-2].get_text(strip=True))
        return last_page_number


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
