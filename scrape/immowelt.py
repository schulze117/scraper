import json
from typing import Any

from bs4 import BeautifulSoup, Tag

from lib.config import get_config, resolve_proxy
from lib.exceptions import ElementNotFoundError, GoneError, InactiveListingError, NotBeautifulSoupError
from lib.models import ListingSource, NextListingModel
from .base import BaseScraper

config = get_config()

# The key the page state sits under. immowelt renamed it on 2026-09-23; the new
# one is stored under the old name, so extract and every earlier row read the
# same shape.
JSON_KEY = "app_cldp"
JSON_KEY_RENAMED = "app_demand_referral_cldp"


class ImmoweltScraper(BaseScraper):
    BASE_URL = "https://www.immowelt.de"

    # SeleniumBase cannot run multiple browser instances concurrently
    CONCURRENT_LISTINGS = False
    # Only re-scrape when the listing has been modified since last scrape
    RESCRAPE_ON_MODIFIED_ONLY = True

    def __init__(self):
        method = config.scrape.immowelt.method
        super().__init__(source=ListingSource.IMMOWELT, method=method, proxy_url=resolve_proxy("scrape", "immowelt"))

    def build_url(self, external_id: str) -> str:
        return f"{self.BASE_URL}/expose/{external_id}"

    def get_minified_html(self, soup: BeautifulSoup) -> str:
        # A deleted expose still renders <main class="Main">, so the page state
        # is the only evidence: it answers 404 or 410 for the classified.
        status = _classified_status(soup)
        if status in (404, 410):
            raise InactiveListingError(f"classified answered {status}")

        minified_html = soup.find("main", class_="Main")

        if minified_html is None:
            raise ElementNotFoundError("Main section")
        if type(minified_html) != Tag:
            raise NotBeautifulSoupError("minified_html")

        minified_html = BeautifulSoup(str(minified_html), "lxml")

        tags_container = minified_html.find("main")
        if tags_container is None:
            raise ElementNotFoundError("Main for custom tags")

        for meta in soup.find_all("meta"):
            tags_container.append(meta)

        for svg in minified_html.find_all("svg"):
            svg.decompose()

        for form in minified_html.find_all("form"):
            form.decompose()

        hidden_elements = minified_html.find_all(class_="HideOnPrint")
        if len(hidden_elements) == 0:
            raise ElementNotFoundError("Hidden elements with class 'print-hide'")
        for hide_on_print in hidden_elements:
            hide_on_print.decompose()

        return str(minified_html.encode(formatter="minimal", indent_level=-50).decode("utf-8"))

    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        json_data_tag = soup.find("script", string=lambda text: text is not None and "__UFRN_LIFECYCLE_SERVERREQUEST__" in text)  # type: ignore

        if json_data_tag is None:
            raise ElementNotFoundError("Script tag containing JSON data")
        if type(json_data_tag) != Tag:
            raise NotBeautifulSoupError("json_data_tag")

        json_data_str = str(json_data_tag).split('JSON.parse("')[1].split('");</script>')[0].strip()
        json_data_str = (
            json_data_str.replace(r"\\", "\\")
            .replace(r"\"", '"')
            .replace(r"\u003cbr/\u003e", " ")
            .replace(r"\u003cbr\u003e", " ")
            .replace(r" ", " ")
        )

        json_data = json.loads(json_data_str)
        if JSON_KEY not in json_data and JSON_KEY_RENAMED in json_data:
            json_data[JSON_KEY] = json_data.pop(JSON_KEY_RENAMED)

        if "app_cldp" not in json_data or not isinstance(json_data["app_cldp"], dict):
            raise ValueError(f"Invalid JSON data format: {json_data}")

        return json_data

    def get_image_urls(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> list[str]:
        gallery = _gallery(json_data)

        if not gallery:
            self.logger.warning("No gallery section found in JSON data")
            return []

        images: list[dict[str, Any]] = gallery.get("images", []) + gallery.get("floorplans", [])
        if not images:
            raise ElementNotFoundError("No images or floorplans found in gallery section")

        return [image["url"] for image in images if "url" in image]

    def get_main_image_url(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> str | None:
        head_info: dict[str, Any] = (
            json_data.get("app_cldp", {}).get("data", {}).get("classified", {}).get("seo", {}).get("headInfo", {})
        )

        if head_info:
            if "openGraphs" in head_info and isinstance(head_info["openGraphs"], list):
                main_image_url = _get_main_image_from_open_graphs(head_info["openGraphs"])
                if main_image_url:
                    return main_image_url

            if "socials" in head_info and isinstance(head_info["socials"], list):
                main_image_url = _get_main_image_from_socials(head_info["socials"])
                if main_image_url:
                    return main_image_url

        self.logger.warning("No headInfo section found in JSON data")

        gallery = _gallery(json_data)

        if not gallery:
            self.logger.warning("No gallery section found in JSON data")
            return None

        for image in gallery.get("images", []) + gallery.get("floorplans", []):
            if "url" in image and (image.get("alt") or image.get("description")):
                return image["url"]

        self.logger.warning("No main image found in gallery section")
        return None

    def is_deactivated_listing(self, exception: Exception, listing: NextListingModel) -> bool:
        self.logger.debug(f"Checking if listing {listing.external_id} is deactivated due to: {exception}")
        # Only on evidence the listing is gone. A missing <main> is not that: it
        # is what a half-loaded page looks like, and a deleted expose has one.
        return isinstance(exception, GoneError)


# --- Helper functions ---

def _classified_status(soup: BeautifulSoup) -> int | None:
    """The HTTP status the page state reports for the classified, if any."""
    tag = soup.find("script", string=lambda text: text is not None and "__UFRN_LIFECYCLE_SERVERREQUEST__" in text)  # type: ignore
    if tag is None:
        return None
    try:
        raw = str(tag).split('JSON.parse("')[1].split('");</script>')[0].strip()
        state = json.loads(raw.replace(r"\\", "\\").replace(r"\"", '"'))
    except (IndexError, ValueError):
        return None
    page = state.get(JSON_KEY) or state.get(JSON_KEY_RENAMED) or {}
    status = ((page.get("data") or {}).get("classified") or {}).get("statusCode")
    return status if isinstance(status, int) else None


def _gallery(json_data: dict[str, Any]) -> dict[str, Any]:
    """Images and floorplans: `sections.gallery` until 2026-09-23, `domains.medias` since."""
    classified = json_data.get(JSON_KEY, {}).get("data", {}).get("classified", {})
    return classified.get("sections", {}).get("gallery") or classified.get("domains", {}).get("medias") or {}


def _get_main_image_from_open_graphs(open_graphs: list[dict[str, Any]]) -> str | None:
    for og in open_graphs:
        if og.get("property") == "image" and "content" in og:
            return og["content"]
    return None


def _get_main_image_from_socials(socials: list[dict[str, Any]]) -> str | None:
    for social in socials:
        if social.get("name") == "twitter:image" and "content" in social:
            return social["content"]
    return None


# --- Entry Point ---
if __name__ == "__main__":
    scraper = ImmoweltScraper()
    scraper.run()
