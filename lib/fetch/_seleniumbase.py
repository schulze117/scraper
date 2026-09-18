import logging
import os
import threading

from seleniumbase import sb_cdp
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_fixed,
)
from lib.config import get_config
from lib.logger import get_logger
from lib.exceptions import BotDetectedError
from lib.helpers import get_network_error, has_bot_detection
from lib.exceptions import FetchNetworkError

config = get_config()
logger = get_logger("_seleniumbase")

# tenacity's before_sleep_log needs an int level; config.log_level is a string.
_LOG_LEVEL_INT = getattr(logging, str(getattr(config, "log_level", "INFO")).upper(), logging.INFO)

# On a bot-detection / captcha page, how many solve+reload cycles to try before
# giving up on this IP (then the workflow re-dispatches on a fresh runner IP).
BOT_SOLVE_ATTEMPTS = 3

# --- Hard-case stealth defaults (mdmintz's advice for the toughest anti-bot) ---
# Pure CDP Mode with a matching timezone/geolocation is the stealthy combo that
# works on x86 GitHub Actions (Azure) IPs using the runner's preinstalled
# google-chrome — the same setup mdmintz uses to bypass DataDome/Walmart there.
# Timezone/geolocation must match the exit IP's country, else it's a tell.
# These are read from config.seleniumbase when present, else fall back to here,
# so the deployed CONFIG_FILE variable does not have to be updated in lockstep.
#
# use_chromium (unbranded Chromium) is an extra lever for the very hardest cases,
# but on x86 ubuntu-latest neither `seleniumbase get chromium` (missing libs) nor
# apt chromium (snap wrapper) launches reliably, so it defaults OFF. Enable it
# only where a working Chromium binary is present (e.g. ARM runners / apt deb).
DEFAULT_USE_CHROMIUM = False
DEFAULT_TZONE = "Europe/Berlin"
DEFAULT_GEOLOC = (52.520008, 13.404954)  # Berlin
DEFAULT_LANG = "de-DE"
# Silent AWS-WAF / anti-bot challenges auto-clear a few seconds after load
# (the challenge JS reloads the page once it issues a token). Wait for that
# before declaring bot detection — the initial page source is often the shim.
#
# From a residential exit IP immoscout serves the *self-clearing* WAF page
# ("Gleich geht's weiter"), not the unsolvable puzzle. Its JS polls for a token
# every 200ms for up to ~10s and then reloads itself, so 8s landed just short of
# the finish line — and reloading it ourselves restarts that clock. Hence: wait
# past the self-reload, then keep polling patiently before escalating.
DEFAULT_INITIAL_WAIT = 12
# How long to let a challenge resolve on its own, and how often to re-check.
CHALLENGE_WAIT = 40
CHALLENGE_POLL_INTERVAL = 4
# Clearing the challenge drops us onto the page mid-render: immoscout serves an
# SSR shell (~37K, IS24.expose present but no #is24-content) and hydrates the
# rest in. Capturing there passes bot detection but loses the content the
# parsers need, which is what "cleared ... then Failed page N" was. So after the
# challenge, poll until the DOM stops growing.
SETTLE_MAX_WAIT = 20
SETTLE_POLL_INTERVAL = 2
# Size-settle alone isn't enough: the shell is briefly stable before the SPA
# swaps in the real content, so two equal reads can land on the shell (seen as
# "cleared ... then Failed page N: Script tag not found", worse on higher-latency
# proxied paths). When the caller knows a string only the finished page contains
# (IS24.resultList for search, IS24.expose for an expose), wait for THAT instead.
CONTENT_MAX_WAIT = 30
CONTENT_POLL_INTERVAL = 3
# Hard wall-clock cap on a single fetch attempt. A stalled proxied CDP call can
# block forever; this must comfortably exceed the worst *legitimate* fetch
# (launch + initial_wait + challenge + content-wait + escalation ≈ 160s) so it
# only ever fires on a true hang. @retry may still retry after it.
FETCH_HARD_TIMEOUT = 180


def _sb_setting(name: str, default):
    """Read a value from config.seleniumbase, falling back to `default` when the
    (possibly older) deployed config.json does not define it."""
    return getattr(config.seleniumbase, name, default)


def _try_solve_captcha(sb) -> str | None:
    """Best-effort click/solve of a captcha widget. seleniumbase's solver only
    handles Cloudflare Turnstile + Google reCAPTCHA (NOT AWS WAF's interactive
    puzzle), so this is a no-op against immoscout's WAF captcha — kept only for
    immowelt / other sites that may surface a solvable challenge. Returns the
    method name used, or None."""
    for name in ("solve_captcha", "gui_click_captcha", "uc_gui_click_captcha"):
        fn = getattr(sb, name, None)
        if callable(fn):
            try:
                fn()
                return name
            except Exception as e:  # method may not apply to this captcha type
                logger.info(f"{name}() did not apply: {e}")
    return None


def _wait_out_challenge(sb, timeout: int = CHALLENGE_WAIT) -> str:
    """Poll the page while a self-clearing challenge resolves, and return the
    resulting HTML. Deliberately does NOT reload: the WAF challenge page reloads
    itself once its JS has a token, and reloading underneath it throws away the
    token it was about to get."""
    html = sb.get_page_source()
    waited = 0
    while has_bot_detection(html) and waited < timeout:
        sb.sleep(CHALLENGE_POLL_INTERVAL)
        waited += CHALLENGE_POLL_INTERVAL
        html = sb.get_page_source()
        if not has_bot_detection(html):
            logger.info(f"Challenge cleared on its own after {waited}s of waiting.")
    return html


def _wait_for_render(sb, html: str, max_wait: int = SETTLE_MAX_WAIT) -> str:
    """Poll until the page stops growing, so we capture the hydrated DOM rather
    than the SSR shell the challenge drops us onto. Returns the settled HTML."""
    waited = 0
    while waited < max_wait:
        sb.sleep(SETTLE_POLL_INTERVAL)
        waited += SETTLE_POLL_INTERVAL
        latest = sb.get_page_source()
        if len(latest) == len(html):
            return latest  # two identical readings — rendering has stopped
        html = latest
    logger.info(f"Page still growing after {max_wait}s; using it as-is ({len(html)} chars).")
    return html


def _wait_for_content(sb, marker: str, html: str, max_wait: int = CONTENT_MAX_WAIT) -> str:
    """Poll until `marker` appears — the reliable signal that the SPA has replaced
    the SSR shell with the real content the parser needs. Returns the html once
    the marker is present, or the last-seen html on timeout (parser then fails
    and the caller logs it, same as before)."""
    if marker in html:
        return html
    waited = 0
    while waited < max_wait:
        sb.sleep(CONTENT_POLL_INTERVAL)
        waited += CONTENT_POLL_INTERVAL
        html = sb.get_page_source()
        if marker in html:
            logger.info(f"Content marker present after {waited}s ({len(html)} chars).")
            return html
    logger.warning(f"Content marker '{marker}' not found after {max_wait}s; capturing anyway ({len(html)} chars).")
    return html


def _finalize_html(sb, html: str, ready_marker: str | None) -> str:
    """After the challenge clears, capture the *complete* page: wait for the
    caller's content marker if one was given, else fall back to size-settle."""
    if ready_marker:
        return _wait_for_content(sb, ready_marker, html)
    return _wait_for_render(sb, html)


def _build_chrome_kwargs(proxy_url: str | None) -> dict:
    """Assemble the sb_cdp.Chrome() kwargs from config with hard-case defaults."""
    incognito = _sb_setting("incognito", True)
    # An authenticated proxy ("user:pass@host:port") is handled by seleniumbase
    # via a generated Chrome extension that supplies the credentials. Chrome does
    # not load extensions into incognito windows, so keeping incognito on here
    # means the proxy silently fails to authenticate. Incognito loses.
    if proxy_url and "@" in proxy_url and incognito:
        logger.info("Authenticated proxy in use — disabling incognito so the "
                    "proxy-auth extension loads.")
        incognito = False

    kwargs: dict = {
        "incognito": incognito,
        "lang": _sb_setting("lang", _sb_setting("locale", DEFAULT_LANG)),
    }
    # Unbranded Chromium. Off by default (see DEFAULT_USE_CHROMIUM); the
    # SeleniumBase stealth docs don't actually list it as an anti-detection
    # measure, so don't reach for it before the exit IP.
    if _sb_setting("use_chromium", DEFAULT_USE_CHROMIUM):
        kwargs["use_chromium"] = True
    # Timezone + geolocation to match the exit IP.
    tzone = _sb_setting("tzone", DEFAULT_TZONE)
    if tzone:
        kwargs["tzone"] = tzone
    geoloc = _sb_setting("geoloc", DEFAULT_GEOLOC)
    if geoloc:
        # config.json parses arrays into lists; start() accepts list | tuple.
        kwargs["geoloc"] = tuple(geoloc) if isinstance(geoloc, (list, tuple)) else geoloc
    # Headed under a virtual display on Linux (CDP mode needs a real display;
    # headless is far less stealthy). Ignored on non-Linux.
    if _sb_setting("xvfb", True):
        kwargs["xvfb"] = True
    if proxy_url:
        # Pure CDP expects "host:port" or "user:pass@host:port" (no scheme).
        kwargs["proxy"] = proxy_url.split("://", 1)[-1]
    return kwargs


@retry(
    stop=stop_after_attempt(config.seleniumbase.max_retries),
    wait=wait_fixed(config.seleniumbase.retry_delay),
    reraise=True,
    # A block already cost BOT_SOLVE_ATTEMPTS rounds of wait-reload-solve inside
    # the fetch; running the whole thing again buys nothing and triples the time
    # the caller needs to notice a wall. At ~2 min per blocked listing that is
    # the difference between spotting a block in minutes and in half an hour.
    retry=retry_if_not_exception_type(BotDetectedError),
    before_sleep=before_sleep_log(logger, _LOG_LEVEL_INT)
)
def get_html_seleniumbase(
    url: str,
    proxy_url: str | None = None,
    timeout: int | None = None,
    screenshot_path: str | None = None,
    on_block: str = "exit",
    ready_marker: str | None = None,
) -> str:
    """Fetch a page with Pure CDP Mode (undetected Chromium) and return its HTML.

    On a suspected bot/captcha page it waits + reloads (and best-effort solves)
    up to BOT_SOLVE_ATTEMPTS times, then os._exit(42) so the workflow re-dispatches
    on a fresh runner IP. `screenshot_path`, when given, saves a PNG of the final
    page — used by the lib.fetch.fetch_url test harness (no overhead in production).

    `on_block` decides what a persistent block does, and the three callers want
    three different things:

      "exit"   os._exit(42) -- the fetch_url harness, where the run IS the probe.
      "raise"  BotDetectedError -- the pipeline. A block must not kill the run
               (one bad IP would cost the whole queue), but it must also never
               be mistaken for content: returning the blocked HTML is what let
               the scrapers read it as a deleted listing and deactivate 19 live
               ones on 2026-09-17. Raising makes that confusion impossible, and
               the caller counts the blocks and decides when to give up.
      "return" the blocked HTML verbatim -- the GCP-proxy probe, whose whole job
               is to look at it and judge the IP.

    The actual work runs on a daemon thread with a hard wall-clock cap. A CDP call
    (get_page_source/reload) can block forever when the proxied connection stalls
    — that froze a scrape run for 54 minutes. On timeout we force-quit the browser
    (which unblocks the stuck call) and raise so @retry can retry; a daemon thread
    can't keep the process alive if the call never returns.
    """
    holder: dict = {"sb": None}
    box: dict = {}

    def _worker():
        try:
            box["html"] = _fetch_body(
                holder, url, proxy_url, timeout, screenshot_path, on_block, ready_marker
            )
        except BaseException as e:  # noqa: BLE001 — surface any failure to the caller
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True, name="sb-fetch")
    t.start()
    t.join(FETCH_HARD_TIMEOUT)
    if t.is_alive():
        logger.error(f"Fetch stalled >{FETCH_HARD_TIMEOUT}s for {url}; force-quitting browser.")
        sb = holder.get("sb")
        if sb is not None:
            try:
                sb.quit()  # closing the browser unblocks the hung CDP call
            except Exception:
                pass
        raise RuntimeError(f"Fetch stalled >{FETCH_HARD_TIMEOUT}s: {url}")
    if "error" in box:
        raise box["error"]
    return box["html"]


def _fetch_body(
    holder: dict,
    url: str,
    proxy_url: str | None,
    timeout: int | None,
    screenshot_path: str | None,
    on_block: str,
    ready_marker: str | None,
) -> str:
    """The real fetch. Runs inside the timeout thread; publishes its browser to
    `holder["sb"]` so the watchdog can force-quit it on a stall."""
    timeout = timeout if timeout is not None else config.seleniumbase.timeout
    initial_wait = _sb_setting("initial_wait", DEFAULT_INITIAL_WAIT)
    chrome_kwargs = _build_chrome_kwargs(proxy_url)

    sb = None
    try:
        sb = sb_cdp.Chrome(url, **chrome_kwargs)
        holder["sb"] = sb
        sb.sleep(initial_wait)  # let a silent WAF challenge issue its token + reload
        # Give a self-clearing challenge every chance to finish before we start
        # poking at it — intervening early is what used to lose these pages.
        html = _wait_out_challenge(sb)

        # The navigation may never have reached the site at all. Chromium's error
        # page is big and marker-free, so without this check it flows on as if it
        # were content and only fails later, in the parser, as a misleading
        # "element not found". Raise instead: @retry gets a chance, and the log
        # names the real problem (a dead proxy tunnel, DNS, no route).
        net_error = get_network_error(html)
        if net_error:
            raise FetchNetworkError(url, net_error)

        if not has_bot_detection(html):
            html = _finalize_html(sb, html, ready_marker)

        if has_bot_detection(html):
            # Try to clear the challenge. Each cycle: best-effort solve, wait for
            # the WAF token / auto-reload, and if still blocked, reload + re-check.
            for solve_attempt in range(1, BOT_SOLVE_ATTEMPTS + 1):
                logger.info(
                    f"Bot detection suspected, attempting to clear "
                    f"(attempt {solve_attempt}/{BOT_SOLVE_ATTEMPTS})..."
                )
                used = _try_solve_captcha(sb)
                # Wait it out again rather than sleeping a fixed 8s: a solve
                # click often just kicks off another token round.
                html = _wait_out_challenge(sb, timeout=16)
                if not has_bot_detection(html):
                    logger.info(
                        f"Bot detection cleared after attempt {solve_attempt} "
                        f"(method: {used or 'wait-only'})."
                    )
                    html = _finalize_html(sb, html, ready_marker)
                    break

                sb.reload(ignore_cache=True)
                sb.sleep(4)
                html = sb.get_page_source()
                if not has_bot_detection(html):
                    logger.info(f"Bot detection cleared after reload (attempt {solve_attempt}).")
                    html = _finalize_html(sb, html, ready_marker)
                    break
            else:
                if screenshot_path:
                    _save_screenshot(sb, screenshot_path)
                    # os._exit below kills the process before the caller can
                    # write anything, so persist the block-page HTML here too.
                    _save_html(html, os.path.splitext(screenshot_path)[0] + ".html")
                if on_block == "return":
                    logger.warning(
                        f"Bot detection persists for {url} after {BOT_SOLVE_ATTEMPTS} "
                        f"attempts (len {len(html)}); returning blocked HTML (probe mode)."
                    )
                    return html
                if on_block == "raise":
                    logger.warning(
                        f"Bot detection persists for {url} after {BOT_SOLVE_ATTEMPTS} "
                        f"attempts (len {len(html)}); raising."
                    )
                    raise BotDetectedError(url, len(html))
                logger.error(
                    f"Bot detection persists after {BOT_SOLVE_ATTEMPTS} solve+reload attempts "
                    f"for {url}. HTML length: {len(html)}. Stopping program."
                )
                print(f"\n{'='*80}")
                print(f"BOT DETECTION PAGE HTML ({url}):")
                print(f"{'='*80}")
                print(html)
                print(f"{'='*80}\n")
                os._exit(42)

        if screenshot_path:
            _save_screenshot(sb, screenshot_path)
        return html
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch {url} with SeleniumBase: {e}")
        raise RuntimeError(f"Failed to fetch {url} with SeleniumBase: {e}")
    finally:
        if sb is not None:
            try:
                sb.quit()
            except Exception:
                pass


def _save_screenshot(sb, path: str) -> None:
    folder = os.path.dirname(path) or "."
    name = os.path.basename(path)
    try:
        os.makedirs(folder, exist_ok=True)
        sb.save_screenshot(name, folder=folder)
        logger.info(f"Saved screenshot to {os.path.join(folder, name)}")
    except Exception as e:
        logger.info(f"Could not save screenshot: {e}")


def _save_html(html: str, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Saved page HTML to {path}")
    except Exception as e:
        logger.info(f"Could not save HTML: {e}")


# test fetch
if __name__ == "__main__":
    test_url = "https://www.immobilienscout24.de/expose/166611357"
    html = get_html_seleniumbase(test_url)
    print(f"Fetched {len(html)} characters from {test_url}")
