import logging
from urllib.parse import urlparse, parse_qs
import requests

log = logging.getLogger("ris_scraper.url")


def fetch(url, timeout):
    """Fetch a URL and return HTML text, or None if non-200 or error."""
    try:
        log.debug("HTTP GET %s", url)
        r = requests.get(url, timeout=timeout)

        if r.status_code == 200:
            return r.text

        log.warning("Non-200 response %s for %s", r.status_code, url)
    except Exception as e:
        log.error("HTTP error for %s: %s", url, e)

    return None


def extract_param(url, name):
    """Extract query parameter from URL, or None if absent."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    if name in params:
        return params[name][0]

    return None
