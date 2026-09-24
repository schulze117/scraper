import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup, Tag

from lib.config import get_config, resolve_proxy
from lib.exceptions import ElementDisabledError, ElementNotFoundError, InactiveListingError, NotBeautifulSoupError
from lib.models import ListingSource, NextListingModel
from .base import BaseScraper

config = get_config()


class KleinanzeigenScraper(BaseScraper):
    BASE_URL = "https://www.kleinanzeigen.de"

    def __init__(self):
        method = config.scrape.kleinanzeigen.method
        super().__init__(source=ListingSource.KLEINANZEIGEN, method=method, proxy_url=resolve_proxy("scrape", "kleinanzeigen"))

    def build_url(self, external_id: str) -> str:
        return f"{self.BASE_URL}/s-anzeige/{external_id}"

    def get_minified_html(self, soup: BeautifulSoup) -> str:
        # A deleted ad redirects to the home page. The fetch does not hand back
        # the final URL, but the page names itself: a live ad's canonical is its
        # /s-anzeige/ URL, the home page's is the site root.
        canonical = soup.find("link", rel="canonical")
        if canonical is not None and "/s-anzeige/" not in (canonical.get("href") or ""):
            raise InactiveListingError(f"redirected to {canonical.get('href')}")

        minified_html = soup.find("section", {"id": "viewad-main"})

        if minified_html is None:
            raise ElementNotFoundError("Main section")
        if type(minified_html) != Tag:
            raise NotBeautifulSoupError("minified_html")

        minified_html = BeautifulSoup(str(minified_html), "lxml")

        contact_button = minified_html.find(
            lambda x: x.name == "button" and x.find("span", string="Nachricht senden") is not None
        )

        if contact_button is None:
            raise ElementNotFoundError("Contact button")
        if type(contact_button) != Tag:
            raise NotBeautifulSoupError("Contact button")
        if contact_button.get("disabled") is not None:
            raise ElementDisabledError("Contact button")

        tags_container = minified_html.find("section")
        if tags_container is None:
            raise ElementNotFoundError("Section for custom tags")

        meta_tags = soup.find_all("meta")
        for meta_tag in meta_tags:
            tags_container.append(meta_tag)

        for script in minified_html.find_all("script"):
            script.decompose()

        return str(minified_html.encode(formatter="minimal", indent_level=-50).decode("utf-8"))

    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        json_data = soup.find("script", string=lambda text: text is not None and "window.BelenConf" in text)  # type: ignore

        if json_data is None:
            raise ElementNotFoundError("Script tag containing JSON data")
        if type(json_data) != Tag:
            raise NotBeautifulSoupError("json_data")

        json_data = str(json_data).split("dimensions: ")[1].split("extraDimensions")[0].strip()[:-1]

        return json.loads(json_data)

    def get_image_urls(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> list[str]:
        image_urls: list[str] = []
        image_tags = soup.find_all("img", {"id": "viewad-image"})

        for img in image_tags:
            if img.get("src") is not None:
                image_urls.append(img.get("src"))

        return image_urls

    def get_main_image_url(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> str | None:
        main_image = soup.find("img", {"id": "viewad-image", "fetchpriority": "high"})

        if main_image is None:
            self.logger.warning(f"Main image not found")
            return None
        if type(main_image) != Tag:
            self.logger.warning(f"Main image not found")
            return None
        if main_image.get("src") is None:
            self.logger.warning(f"Main image not found")
            return None

        image_source = main_image.get("src")
        if type(image_source) != str:
            raise NotBeautifulSoupError("main_image.src")

        return image_source

    def get_extra_data(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        extra_info_tag = soup.find("div", {"id": "viewad-extra-info"})
        if extra_info_tag is None:
            raise ElementNotFoundError("Extra info tag")

        date_span = extra_info_tag.find("span")
        if not date_span:
            raise ElementNotFoundError("Date span in extra info tag")
        if type(date_span) != Tag:
            raise NotBeautifulSoupError("date_span")

        created_at = date_span.get_text(strip=True)
        if not created_at:
            raise ValueError("Created at text is empty")

        return {
            "property": {
                "created_at": datetime.strptime(created_at, "%d.%m.%Y"),
            },
        }

    def is_deactivated_listing(self, exception: Exception, listing: NextListingModel) -> bool:
        self.logger.debug(f"Checking if listing {listing.external_id} is deactivated due to: {exception}")
        # Only on evidence from the ad page itself. A missing main section is not
        # that -- a deleted ad is caught by its redirect in get_minified_html.
        return bool(listing) and '"Contact button" is disabled' in str(exception)


# --- Entry Point ---
if __name__ == "__main__":
    scraper = KleinanzeigenScraper()
    scraper.run()
