class ScrapeError(Exception):
    """Base class for all scrape-related exceptions."""


class GoneError(ScrapeError):
    """Raised when a listing is no longer available."""

    def __init__(self, item_name: str):
        super().__init__(f'Listing "{item_name}" is gone')


class ElementNotFoundError(ScrapeError):
    """Raised when a required element is not found in the HTML content."""

    def __init__(self, item_name: str):
        super().__init__(f'Element "{item_name}" not found')


class ElementDisabledError(ScrapeError):
    """Raised when an element is found but is disabled."""

    def __init__(self, item_name: str):
        super().__init__(f'Element "{item_name}" is disabled')


class NotBeautifulSoupError(ScrapeError):
    """Raised when an expected BeautifulSoup object is not found."""

    def __init__(self, item_name: str):
        super().__init__(f'Element "{item_name}" is not a BeautifulSoup object')


class StructureChangedError(ScrapeError):
    """Raised when a page loaded and its container parsed, but the content inside
    no longer has the shape the parser expects.

    Distinct from ElementNotFoundError by where the change lands. A missing
    container fails on its own. A renamed field inside intact markup does not —
    it yields an *empty* result, which reads exactly like a quiet day:
    process_page_strategy scores it as new_count=0, the early-stop streak accepts
    it, and the run exits green having found nothing. Empty is therefore the
    dangerous case, and the parser has to raise rather than return it. That turns
    it into new_count=None, which fails the page and colours the run red.
    """

    def __init__(self, item_name: str, detail: str):
        super().__init__(f'Structure of "{item_name}" changed: {detail}')
        self.item_name = item_name
        self.detail = detail


class ResultTailReachedError(ScrapeError):
    """Raised when a result page holds only well-formed but *undated* entries.

    Not a defect — the end of the result set. Immoscout is crawled newest-first
    (`sorting=2`), which sorts on the creation date, so every entry that has no
    `@creation` is pushed behind every entry that has one. Those undated entries
    are real offers of a kind we deliberately do not store (`tenantNetwork`
    exchange flats, `draftListing` drafts), and they form one contiguous block at
    the very end of every category.

    Measured on WOHNUNG_MIETEN, 2026-09-14: page 226 was 20/20 dated and all 20
    already in the database; pages 230 and 300 were 0/20 dated and 0/20 in the
    database. The portal advertises 518 pages for a live inventory that ends
    around page 227.

    Telling this apart from StructureChangedError is the whole point. Both look
    like "nothing parsed": one means the listing set ran out, the other means a
    field was renamed and we are about to lose everything. The discriminator is
    that a tail entry is otherwise intact — it keeps `@id`, `@modification` and
    its `resultlist.realEstate` body, and only the creation date is absent.
    """

    def __init__(self, item_name: str, detail: str):
        super().__init__(f'Result tail reached in "{item_name}": {detail}')
        self.item_name = item_name
        self.detail = detail


class InactiveListingError(ScrapeError):
    """Raised when a listing is inactive."""

    def __init__(self, external_id: str):
        super().__init__(f'Listing "{external_id}" is inactive')


class ExecutionStoppedError(Exception):
    """Raised when the execution is stopped."""

    def __init__(self, message: str = "Execution stopped"):
        super().__init__(message)


class ServerError(Exception):
    """Raised when a server error occurs."""

    def __init__(self, url: str, status_code: int, message: str = "Server error occurred"):
        super().__init__(f'"{message}" for URL {url} with status code {status_code}')
        self.url = url
        self.status_code = status_code


class HTMLValidationError(Exception):
    """Raised when the HTML content is invalid or empty."""

    def __init__(self, message: str = "HTML content is invalid or empty"):
        super().__init__(message)


class FetchNetworkError(Exception):
    """Raised when the browser never reached the site — Chromium served its own
    error page instead. Distinct from bot detection on purpose: a block means
    the site refused us, this means we could not get there (dead proxy tunnel,
    DNS, no route). Conflating the two sent us hunting for a WAF change while
    the real fault was an unroutable proxy host.
    """

    def __init__(self, url: str, code: str):
        super().__init__(f"Could not reach {url}: browser reported {code}")
        self.url = url
        self.code = code


class BotDetectedError(Exception):
    """Raised when the portal served an anti-bot block page instead of content.

    The distinction from every other fetch failure is the whole point. A block
    page parses like a deleted listing -- no <main>, no JSON -- and the scrapers
    read a missing main section as "the ad is gone" and deactivate. On
    2026-09-17 that turned a 40-minute DataDome wall into 19 listings marked
    offline that nobody had ever looked at.

    So a block must never reach a parser. It is raised here, and
    `is_deactivated_listing` is never consulted for it.
    """

    def __init__(self, url: str, html_len: int):
        super().__init__(f"Bot detection page served for {url} (len {html_len})")
        self.url = url
        self.html_len = html_len


class FinderFailedError(Exception):
    """Raised at the end of a find run that lost one or more categories.

    Page 1 carries the page count, so a category whose page 1 never loads is
    skipped whole. That is silent data loss, and it must colour the workflow
    run red — a green tick on a zero-listing run is how immoscout stayed dead
    for two days in August 2026.
    """

    def __init__(self, lost: list[str], total_new: int):
        super().__init__(
            f"{len(lost)} category/location crawls lost page 1 and were skipped "
            f"({', '.join(lost)}); {total_new} new listings saved this run."
        )
        self.lost = lost
        self.total_new = total_new
