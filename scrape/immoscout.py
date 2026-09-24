from typing import Any

import demjson3  # type: ignore
from bs4 import BeautifulSoup, Tag

from lib.config import get_config, resolve_proxy
from lib.exceptions import (
    ElementNotFoundError,
    GoneError,
    InactiveListingError,
    NotBeautifulSoupError,
)
from lib.models import ListingSource, NextListingModel
from .base import BaseScraper

config = get_config()

VALID_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]
DEACTIVATED_MESSAGES = ["Angebot wurde deaktiviert", "Angebot nicht gefunden"]


class ImmoscoutScraper(BaseScraper):
    BASE_URL = "https://www.immobilienscout24.de"

    # Only re-scrape when the listing has been modified since last scrape
    RESCRAPE_ON_MODIFIED_ONLY = True
    # One browser at a time — serial is gentler on the single residential proxy
    # and matches the finder. (max_workers=1 already made it serial; explicit here.)
    CONCURRENT_LISTINGS = False
    # Wait for the #is24-content div, NOT "IS24.expose": the JSON var is already
    # in the ~32K SSR shell, but get_minified_html needs the rendered
    # #is24-content div (and .print-hide), which only appears in the fully
    # hydrated ~373K page. Waiting on IS24.expose captured the shell and every
    # scrape failed with "Main section not found".
    READY_MARKER = 'id="is24-content"'

    def __init__(self):
        method = config.scrape.immoscout.method
        super().__init__(source=ListingSource.IMMOBILIENSCOUT24, method=method, proxy_url=resolve_proxy("scrape", "immoscout"))

    def build_url(self, external_id: str) -> str:
        return f"{self.BASE_URL}/expose/{external_id}"

    def get_minified_html(self, soup: BeautifulSoup) -> str:
        status_container = soup.find("div", class_="status-message")
        if status_container and any(msg in status_container.get_text() for msg in DEACTIVATED_MESSAGES):
            raise InactiveListingError("Listing is deactivated or not found")

        minified_html = soup.find("div", {"id": "is24-content"})

        if minified_html is None:
            raise ElementNotFoundError("Main section")
        if type(minified_html) != Tag:
            raise NotBeautifulSoupError("minified_html")

        minified_html = BeautifulSoup(str(minified_html), "lxml")

        for svg in minified_html.find_all("svg"):
            svg.decompose()
        for form in minified_html.find_all("form"):
            form.decompose()
        for style in minified_html.find_all("style"):
            style.decompose()
        for script in minified_html.find_all("script"):
            script.decompose()
        for img in minified_html.find_all("img"):
            img.decompose()

        hidden_elements = minified_html.find_all(class_="print-hide")
        if len(hidden_elements) == 0:
            raise ElementNotFoundError("Hidden elements with class 'print-hide'")
        for print_hide in hidden_elements:
            print_hide.decompose()

        return str(minified_html.encode(formatter="minimal", indent_level=-50).decode("utf-8"))

    def get_json_data(self, soup: BeautifulSoup) -> dict[str, Any]:
        json_script_tag = soup.find("script", string=lambda text: text is not None and "IS24.expose" in text)  # type: ignore

        if json_script_tag is None:
            raise ElementNotFoundError("Script tag containing JSON data")
        if type(json_script_tag) != Tag:
            raise NotBeautifulSoupError("json_script")

        json_script_str = (
            str(json_script_tag)
            .split("IS24.expose = ")[1]
            .split("IS24.reporting")[0]
            .strip()[:-1]
            .replace(": undefined", ": null")
        )
        json_data: dict[str, Any] = demjson3.decode(json_script_str)  # type: ignore

        if not isinstance(json_data, dict):
            raise ValueError("Decoded JSON data is not a dictionary")

        coordinates_script_tag = (
            soup.find("script", string=lambda text: text is not None and "IS24.expose.quickCheckConfig" in text)  # type: ignore
            or soup.find("script", string=lambda text: text is not None and "quarterBounds" in text)  # type: ignore
        )

        if coordinates_script_tag is None:
            raise ElementNotFoundError("Script tag containing coordinates data")
        if type(coordinates_script_tag) != Tag:
            raise NotBeautifulSoupError("coordinates_script_tag")

        coordinates_script_str = str(coordinates_script_tag)
        if '"quickCheckServiceUrl": ' in coordinates_script_str:
            coordinates_data = coordinates_script_str.split('"quickCheckServiceUrl": ')[1].split(",")[0].strip()
        elif "lat:" in coordinates_script_str and "lng:" in coordinates_script_str:
            coordinates_data = " ".join(
                line.split(":")[1].strip()
                for line in coordinates_script_str.splitlines()
                if "lat:" in line or "lng:" in line
            )
        elif "quarterBoundsJson" in coordinates_script_str:
            coordinates_data = coordinates_script_str.split('"quarterBoundsJson":')[1].split("}}")[0].strip() + "}}"
        else:
            coordinates_data = coordinates_script_str.split("quarterBounds: ")[1].split("}},")[0].strip() + "}}"

        json_data["locationAddress"]["coordinates"] = coordinates_data

        return json_data

    def get_image_urls(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> list[str]:
        images: list[dict[str, Any]] = json_data.get("galleryData", {}).get("images", [])

        if not images:
            self.logger.warning("No images found in JSON data")
            return []

        image_urls: list[str] = []
        for image in images:
            if "fullSizePictureUrl" in image:
                image_url: str = image["fullSizePictureUrl"]
                image_url = image_url.split("/ORIG/")[0]
                if not any(image_url.endswith(ext) for ext in VALID_IMAGE_EXTENSIONS):
                    raise ValueError(f"Invalid image URL format: {image_url}")
                image_urls.append(image_url)

        return image_urls

    def get_main_image_url(self, soup: BeautifulSoup, json_data: dict[str, Any]) -> str | None:
        images: list[dict[str, Any]] = json_data.get("galleryData", {}).get("images", [])

        if not images:
            self.logger.warning("No images found in JSON data")
            return None

        for image in images:
            if "fullSizePictureUrl" in image and image.get("id") == "1":
                image_url: str = image["fullSizePictureUrl"]
                image_url = image_url.split("/ORIG/")[0]
                if not any(image_url.endswith(ext) for ext in VALID_IMAGE_EXTENSIONS):
                    raise ValueError(f"Invalid image URL format: {image_url}")
                return image_url

        self.logger.warning("No main image found in gallery data")
        return None

    def is_deactivated_listing(self, exception: Exception, listing: NextListingModel) -> bool:
        self.logger.debug(f"Checking if listing {listing.external_id} is deactivated due to: {exception}")
        # Only on evidence the listing is gone (the status message, see
        # get_minified_html). A missing #is24-content is a page that did not
        # finish rendering.
        return isinstance(exception, (GoneError, InactiveListingError))


# --- Entry Point ---
if __name__ == "__main__":
    scraper = ImmoscoutScraper()
    scraper.run()
