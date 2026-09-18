import atexit

from lib.fetch._curl_cffi import get_html_curlcffi
from lib.fetch._seleniumbase import get_html_seleniumbase
from lib.proxy import FirewallManager

from lib.config import get_config, env_bool
from lib.exceptions import BotDetectedError
from lib.helpers import expand_proxy_url, has_bot_detection, redact_proxy
from lib.logger import get_logger

config = get_config()
logger = get_logger("fetcher")

class Fetcher:
    def __init__(self, method: str, proxy_url: str | None = None):
        """
        :param method: "curl_cffi", "playwright", etc.
        :param proxy_url: The full proxy string (e.g. http://user:pass@ip:port) or None
        """
        self.method = method
        self.proxy_url = proxy_url
        
        # --- FIREWALL INTEGRATION ---
        # Only for the self-hosted GCP proxy, which allow-lists the caller's IP.
        # Third-party proxies (e.g. DataImpulse) need none of this, and running
        # it without GCP credentials present just throws — hence opt-in.
        self._fw_manager = None
        if self.proxy_url and env_bool("PROXY_MANAGE_FIREWALL", False):
            try:
                logger.info("Proxy detected. initializing firewall...")
                self._fw_manager = FirewallManager()
                self._fw_manager.authorize_current_ip()
            except Exception as e:
                logger.warning(f"Could not update firewall rules: {e}")
                # We don't raise here, in case the rule already exists
                # or we want to try fetching anyway.
        elif self.proxy_url:
            logger.info(f"Using proxy {redact_proxy(self.proxy_url)}")

    def fetch(self, url: str, ready_marker: str | None = None) -> str:
        """
        Determines proxy, selects method, and returns HTML string.

        `ready_marker`: a string that only the fully-rendered page contains
        (e.g. "IS24.resultList"). The browser fetcher waits for it before
        capturing, so we don't grab the SSR shell. Ignored by curl_cffi (no JS).
        """
        # Draw a fresh sticky session per fetch. The session must hold still for
        # the duration of one page (the WAF token is bound to the exit IP), but
        # pinning the whole run to a single IP means one bad draw stalls every
        # remaining page — a scrape run sat on 0/8 listings for 40 minutes that
        # way. Per fetch: stable where it matters, self-healing across pages.
        proxy_url = expand_proxy_url(self.proxy_url) if self.proxy_url else None

        if self.method == "curl_cffi":
            html = get_html_curlcffi(url, proxy_url=proxy_url)
            # curl_cffi has no challenge-solving stage to notice a block, and a
            # WAF that answers 200 with a shim slips past the status check. Judge
            # it here so this path cannot hand a block page to a parser either.
            if has_bot_detection(html):
                raise BotDetectedError(url, len(html))
            return html

        elif self.method == "playwright":
            raise NotImplementedError("Playwright fetcher is not yet implemented.")

        elif self.method == "seleniumbase":
            # on_block="raise": a blocked page must not os._exit and kill the
            # whole finder/scraper run, but it must not be returned as content
            # either. It used to be, and the parser's "no <main> section" then
            # read as "the ad is gone" — see BotDetectedError.
            return get_html_seleniumbase(
                url, proxy_url=proxy_url, ready_marker=ready_marker, on_block="raise"
            )

        else:
            raise ValueError(f"Unknown fetching method in config: {self.method}")
