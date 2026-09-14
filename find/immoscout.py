import json
from datetime import datetime
from typing import Any
from bs4 import BeautifulSoup, Tag
from lib.config import get_config, resolve_proxy
from lib.models import IMMOSCOUT_SEARCH_CATEGORIES, ListingSource, NewListing
from lib.exceptions import (
    ElementNotFoundError,
    NotBeautifulSoupError,
    ResultTailReachedError,
    StructureChangedError,
)
from .base import BaseFinder, run_finder

config = get_config()

# Everything extract_listing_data needs. An entry missing one of these is skipped,
# not fatal — IS24 serves the occasional new-build object without @creation.
REQUIRED_LISTING_KEYS = frozenset({"@id", "@modification", "@creation"})

def has_listing_keys(entry: Any) -> bool:
    return isinstance(entry, dict) and REQUIRED_LISTING_KEYS <= entry.keys()

# What an entry keeps when it is merely undated rather than broken: its id, its
# edit date and its body. Only `@creation` is missing.
TAIL_LISTING_KEYS = frozenset({"@id", "@modification", "resultlist.realEstate"})

def is_undated_entry(entry: Any) -> bool:
    """A well-formed result entry that simply carries no creation date.

    These are real offers — `tenantNetwork` exchange flats and `draftListing`
    drafts — and because the crawl sorts newest-first on exactly that date, they
    all land behind every dated listing. So a page made entirely of them is the
    end of the result set, not a parser problem. Requiring the rest of the entry
    to be intact is what keeps this apart from `@creation` being renamed away:
    a rename leaves the body intact too, but it would strike page 1 as readily as
    page 300, which is why BaseFinder also demands depth before believing this.
    """
    return (isinstance(entry, dict)
            and "@creation" not in entry
            and TAIL_LISTING_KEYS <= entry.keys())

def tail_markers(entries: list[Any]) -> str:
    """Which undated kinds this page held — logged, never used as the condition.

    `tenantNetwork` and `draftListing` are the two we have seen. Reading them out
    means a third kind shows up in the logs the day it appears, without the
    detection depending on a list we would have to keep current.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = next((k for k in ("tenantNetwork", "draftListing", "project") if entry.get(k)), "untagged")
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{n}x {k}" for k, n in sorted(counts.items()))

def get_similar_entries(entry: dict[str, Any]) -> list[Any]:
    """The similarObject dicts nested under one result entry.

    similarObjects is a bonus field, so no shape it arrives in may take the page
    down with it — the main entries on that page are the data we actually came
    for. Returns [] for anything unexpected rather than raising.
    """
    groups = entry.get("similarObjects")
    if isinstance(groups, dict):
        groups = [groups]
    if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
        return []

    similar = groups[0].get("similarObject")
    if isinstance(similar, dict):
        return [similar]
    return similar if isinstance(similar, list) else []

def extract_listing_data(listing: dict[str, Any]) -> NewListing:
    modified_at = listing.get("@modification")
    created_at = listing.get("@creation")

    if not modified_at:
        raise ValueError(f"@modification not found in listing data: {listing}")
    if not created_at:
        raise ValueError(f"@creation not found in listing data: {listing}")

    return NewListing(
        external_id=listing["@id"],
        created_at=datetime.fromisoformat(created_at),
        modified_at=datetime.fromisoformat(modified_at),
        source=ListingSource.IMMOBILIENSCOUT24,
    )

class ImmoscoutFinder(BaseFinder):
    SOURCE = ListingSource.IMMOBILIENSCOUT24
    CONCURRENT_LOCATIONS = False
    BASE_URL = "https://www.immobilienscout24.de/Suche/shape"
    # After the WAF challenge clears, wait until the results JSON is actually in
    # the page (it hydrates in after the SSR shell) before capturing — see
    # get_json_data, which parses IS24.resultList.
    READY_MARKER = "IS24.resultList"
    # Newest-first, ~453 pages/category at ~30s each through the proxy — stop once
    # 3 consecutive pages have no new listings (page 1 carries pinned promoted
    # listings we usually already know, so a 1-page stop quit far too early), and
    # never crawl past MAX_PAGES as a safety net.
    STOP_WHEN_NO_NEW = True
    NO_NEW_PAGES_TO_STOP = 3
    MAX_PAGES = 40
    # A results page holds 20 entries. Fewer means the result set really does end
    # here; a full one with no pagination cannot — see get_pages_count.
    FULL_PAGE_ENTRIES = 20

    def __init__(self):
        method = config.find.immoscout.method
        super().__init__(method=method, proxy_url=resolve_proxy("find", "immoscout"))

    def get_categories(self):
        return IMMOSCOUT_SEARCH_CATEGORIES.items()

    def get_locations(self):
        return self.config.finder.locations.immoscout

    def build_url(self, category: str, location: str, page: int = 0) -> str:
        url = f"{self.BASE_URL}/{category}?shape={location}&enteredFrom=result_list&sorting=2"
        if page > 1:
            url += f"&pagenumber={page}"
        return url

    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        json_script_tag = soup.find("script", string=lambda text: text is not None and "IS24.resultList" in text)  # type: ignore

        if json_script_tag is None:
            raise ElementNotFoundError("Script tag containing JSON data")
        if type(json_script_tag) != Tag:
            raise NotBeautifulSoupError("json_script")

        json_data: str = (
            str(json_script_tag)
            .split("resultListModel: ")[1]
            .split("isUserLoggedIn")[0]
            .strip()[:-1]
            .replace(": undefined", ": null")
        )
        

        return json.loads(json_data)

    def get_result_entries(self, soup: BeautifulSoup) -> list[dict[str, Any]]:
        """The raw resultlistEntry list, normalised to a list.

        The JSON is converted from XML, so a page holding a single hit collapses
        resultlistEntry to a bare dict instead of a one-element list. Iterating
        that yields key strings, which look like unparsable entries — and with
        get_listings now raising on "nothing parsed", that would fail a perfectly
        good page.
        """
        json_data = self.get_json_data(soup)
        result_list = json_data["searchResponseModel"]["resultlist.resultlist"]["resultlistEntries"][0]

        entries = result_list.get("resultlistEntry")
        if isinstance(entries, dict):
            return [entries]
        return entries if isinstance(entries, list) else []

    def get_listings(self, soup: BeautifulSoup) -> list[NewListing]:
        result_entries = self.get_result_entries(soup)
        if not result_entries:
            self.logger.warning("No listings found on this page, skipping")
            return []

        listings: list[NewListing] = []
        skipped = 0

        for entry in result_entries:
            if not has_listing_keys(entry):
                skipped += 1
                continue
            listings.append(extract_listing_data(entry))

            # The same guard as above. This branch used to check "@id" alone, so
            # a similar object without @creation reached extract_listing_data,
            # raised, and cost the whole category its page 1.
            for similar_entry in get_similar_entries(entry):
                if not has_listing_keys(similar_entry):
                    skipped += 1
                    continue
                listings.append(extract_listing_data(similar_entry))

        # Nothing parsed. That is either the end of the dated result set or a
        # renamed field, and the two must not be confused: one is the crawl
        # finishing, the other is the crawl silently losing everything.
        #
        # Undated-but-intact across the whole page says tail. BaseFinder decides
        # whether to believe it — it wants three such pages in a row and real
        # depth first, because a renamed `@creation` would make page 1 look
        # exactly like this.
        if not listings and result_entries and all(is_undated_entry(e) for e in result_entries):
            raise ResultTailReachedError(
                "resultlistEntry",
                f"all {len(result_entries)} entries intact but undated ({tail_markers(result_entries)})",
            )

        # Skipping a stray entry is routine; skipping every one of them is a
        # renamed field, and it must not pass as "no new listings" — that scores
        # as new_count=0, feeds the early-stop streak, and exits the run green.
        if not listings:
            raise StructureChangedError(
                "resultlistEntry",
                f"{len(result_entries)} entries on the page, none carrying {sorted(REQUIRED_LISTING_KEYS)}",
            )
        if skipped:
            self.logger.warning(
                f"Skipped {skipped} unparsable entries of {len(result_entries)} on the page "
                f"(missing {sorted(REQUIRED_LISTING_KEYS)}) — a rising share is the warning "
                f"before it reaches all of them."
            )

        return listings

    def get_pages_count(self, soup: BeautifulSoup) -> int:
        pagination_buttons = soup.find_all(attrs={"data-testid": "pagination-button"})
        if len(pagination_buttons) >= 2:
            return int(pagination_buttons[-1].get_text(strip=True))

        # No pagination on this page: either the result set fits on one page, or
        # data-testid moved and the page count is no longer visible to us.
        # Answering 1 to both used to cap every category at page 1 while the run
        # still exited green — up to MAX_PAGES worth of listings, silently gone.
        # A full page cannot be a one-page result set, so that combination is the
        # selector breaking and has to fail loudly. Re-parsing the JSON here costs
        # milliseconds against a ~30 s fetch.
        # The trade is deliberate: a category with exactly FULL_PAGE_ENTRIES hits
        # and no pagination trips this falsely, costing one red run that fixes
        # itself on the next crawl. The silent version costs weeks of missing data.
        entry_count = len(self.get_result_entries(soup))
        if entry_count >= self.FULL_PAGE_ENTRIES:
            raise StructureChangedError(
                "pagination-button",
                f"{entry_count} entries on the page but no pagination buttons",
            )

        self.logger.info(f"No pagination buttons, {entry_count} entries — single-page result set.")
        return 1

# --- Entry Point ---
if __name__ == "__main__":
    run_finder(ImmoscoutFinder)