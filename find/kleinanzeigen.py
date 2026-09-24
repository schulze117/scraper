from bs4 import BeautifulSoup, Tag
from lib.config import get_config, resolve_proxy
from lib.database import Database
from lib.models import KLEINANZEIGEN_SEARCH_CATEGORIES, ListingSource, NewListing
from lib.exceptions import ElementNotFoundError, NotBeautifulSoupError, StructureChangedError
from .base import BaseFinder, run_finder

config = get_config()

# What the result counter says when a search has no hits. The result list is then
# left out of the page altogether -- that is the one case where a missing list
# is not a structure change.
EMPTY_SEARCH_MARKER = "Es wurden keine"

class KleinanzeigenFinder(BaseFinder):
    SOURCE = ListingSource.KLEINANZEIGEN
    LISTINGS_PER_PAGE = 25
    CONCURRENT_LOCATIONS = True
    BASE_URL = "https://www.kleinanzeigen.de/"
    # The slug segment every search URL needs. Kleinanzeigen dropped the
    # slug-less form on 2026-09-17 -- /c203l4772 used to redirect to the
    # canonical URL and now 404s, which stalled the finder for six hours on
    # nothing but retries. The segment's content is ignored (the portal serves
    # the category named by the id even when the slug contradicts it), but it
    # must be present and must start with "s-". This is the slug the portal's
    # own `rel=canonical` carries, and it is the same for all four categories.
    SEARCH_PATH = "s-immobilien"

    def __init__(self):
        method = config.find.kleinanzeigen.method
        super().__init__(method=method, proxy_url=resolve_proxy("find", "kleinanzeigen"))

    def get_categories(self):
        return KLEINANZEIGEN_SEARCH_CATEGORIES.items()

    def get_locations(self):
        ids_from_config = set(self.config.finder.locations.kleinanzeigen.ids)
        ids_from_states = {
            kid 
            for state in self.config.finder.locations.kleinanzeigen.states 
            for kid in self.db.get_kleinanzeigen_ids_by_state(state)
        }
        return list(ids_from_config | ids_from_states)

    def build_url(self, category_id, location, page):
        page_path = f"seite:{page}/" if page > 1 else ""
        return f"{self.BASE_URL}{self.SEARCH_PATH}/{page_path}c{category_id}l{location}"

    def is_empty_search(self, soup: BeautifulSoup) -> bool:
        counter = soup.find(id="srp-breadcrumb-summary")
        return isinstance(counter, Tag) and EMPTY_SEARCH_MARKER in counter.get_text()

    def get_listings(self, soup: BeautifulSoup) -> list[NewListing]:
        entries_list = soup.find("ul", attrs={"id": "srchrslt-adtable"})

        # Returning [] here used to cover every reason the list might be missing,
        # and [] scores as "no new listings" -- a renamed list would have run
        # green with nothing found. A search without hits leaves the list out
        # and says so in the counter (the "Umkreis" list that shows up instead is
        # #srchrslt-adtable-altads and never was ours); any other page without it
        # is the markup having moved.
        if not entries_list:
            if self.is_empty_search(soup):
                return []
            raise StructureChangedError(
                "ul#srchrslt-adtable", "no result list, and the counter does not report an empty search")
        if type(entries_list) != Tag:
            raise NotBeautifulSoupError("entries_list")

        listings: list[NewListing] = []
        skipped = 0

        for entry in entries_list.find_all("article", attrs={"data-adid": True}):
            external_id = entry.get("data-adid")
            if not external_id:
                skipped += 1
                continue
            listing = NewListing(external_id=external_id, source=ListingSource.KLEINANZEIGEN)

            if listing.external_id in [l.external_id for l in listings]:
                self.logger.debug(f"Duplicate listing found, skipping: {listing.external_id}")
                continue

            listings.append(listing)

        # The list is only rendered when there are hits, so a list with nothing
        # readable in it is a renamed attribute or tag, not a quiet page.
        if not listings:
            raise StructureChangedError(
                "article[data-adid]",
                f"result list present with {len(entries_list.find_all('article'))} articles, "
                f"none carrying a data-adid",
            )
        if skipped:
            self.logger.warning(
                f"Skipped {skipped} entries without a data-adid on the page — a rising share "
                f"is the warning before it reaches all of them."
            )

        return listings

    def get_pages_count(self, soup: BeautifulSoup) -> int:
        # Renamed in the same 2026-09-17 relaunch that killed the slug-less URL:
        # the result counter used to be span.breadcrump-summary and is now
        # #srp-breadcrumb-summary (the typo fixed along with it). Matched on the
        # id alone, not the tag, because the new markup is Tailwind-generated and
        # its classes look generated too -- the id is the only stable handle.
        # The text it carries is unchanged apart from a leading range:
        # "1 - 25 von 52 Mietwohnungen in ...", so the parsing below still holds.
        total_listings_tag = soup.find(id="srp-breadcrumb-summary")

        if not total_listings_tag:
            raise ElementNotFoundError("#srp-breadcrumb-summary")
        if type(total_listings_tag) != Tag:
            raise NotBeautifulSoupError("total_listings_tag")
        if self.is_empty_search(soup):
            return 0

        total_listings_text = total_listings_tag.get_text(strip=True)
        if not total_listings_text:
            raise ValueError("Total listings text is empty")

        # The counter is the only page count this portal gives us. An unreadable
        # one used to log and answer 0, which ends the location after page 1
        # while the run stays green -- the kleinanzeigen form of a full page
        # without pagination. The empty search is handled above.
        if "von " not in total_listings_text:
            raise StructureChangedError("#srp-breadcrumb-summary", f"no 'von <n>' in {total_listings_text!r}")
        total_listings = total_listings_text.split("von ")[1].split(" ")[0].strip().replace(".", "")
        if not total_listings.isdigit():
            raise StructureChangedError("#srp-breadcrumb-summary", f"hit count {total_listings!r} is not a number")

        total_listings = int(total_listings)
        self.logger.debug(f"Total listings: {total_listings}")

        pages = total_listings // self.LISTINGS_PER_PAGE

        if total_listings % self.LISTINGS_PER_PAGE != 0:
            pages += 1

        if pages > 50:
            self.logger.warning(f"Total pages {pages} exceeds the maximum limit of 50, setting to 50")
            pages = 50

        self.logger.debug(f"Total pages: {pages}")

        return pages


# --- Entry Point ---
if __name__ == "__main__":
    run_finder(KleinanzeigenFinder)
