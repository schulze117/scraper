from bs4 import BeautifulSoup, Tag
from lib.config import get_config, resolve_proxy
from lib.database import Database
from lib.models import KLEINANZEIGEN_SEARCH_CATEGORIES, ListingSource, NewListing
from lib.exceptions import ElementNotFoundError, NotBeautifulSoupError
from .base import BaseFinder, run_finder

config = get_config()

class KleinanzeigenFinder(BaseFinder):
    SOURCE = ListingSource.KLEINANZEIGEN
    LISTINGS_PER_PAGE = 25
    CONCURRENT_LOCATIONS = True
    BASE_URL = "https://www.kleinanzeigen.de/"

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
        return f"{self.BASE_URL}/{page_path}c{category_id}l{location}"

    def get_listings(self, soup: BeautifulSoup) -> list[NewListing]:
        entries_list = soup.find("ul", attrs={"id": "srchrslt-adtable"})

        if not entries_list:
            return []
        if type(entries_list) != Tag:
            raise NotBeautifulSoupError("entries_list")

        listings: list[NewListing] = []

        for entry in entries_list.find_all("article", attrs={"data-adid": True}):
            external_id = entry.get("data-adid")
            if not external_id:
                self.logger.warning(f"No data-adid found in entry: {entry}")
                continue
            listing = NewListing(external_id=external_id, source=ListingSource.KLEINANZEIGEN)

            if listing.external_id in [l.external_id for l in listings]:
                self.logger.debug(f"Duplicate listing found, skipping: {listing.external_id}")
                continue

            listings.append(listing)

        return listings

    def get_pages_count(self, soup: BeautifulSoup) -> int:
        total_listings_tag = soup.find("span", class_="breadcrump-summary")

        if not total_listings_tag:
            raise ElementNotFoundError("span.breadcrump-summary")
        if type(total_listings_tag) != Tag:
            raise NotBeautifulSoupError("total_listings_tag")
        if "Es wurden keine" in total_listings_tag.get_text():
            return 0

        total_listings_text = total_listings_tag.get_text(strip=True)
        if not total_listings_text:
            raise ValueError("Total listings text is empty")

        total_listings = total_listings_text.split("von ")[1].split(" ")[0].strip().replace(".", "")
        if not total_listings.isdigit():
            self.logger.warning(f"Total listings is not a valid number, assuming 0: {total_listings}")
            return 0

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
