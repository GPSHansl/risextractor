import time
import requests
import yaml
import logging
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from pymongo import MongoClient
from pymongo.errors import PyMongoError

DEFAULT_CONFIG = "/config/defaults.yml"
RUNTIME_CONFIG = "/config/runtime.yml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("ris_scraper")


def load_config():

    log.info("Loading configuration")

    with open(DEFAULT_CONFIG) as f:
        defaults = yaml.safe_load(f)

    with open(RUNTIME_CONFIG) as f:
        runtime = yaml.safe_load(f)

    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and k in a:
                merge(a[k], v)
            else:
                a[k] = v
        return a

    cfg = merge(defaults, runtime)

    log.info("Configuration loaded")

    return cfg


config = load_config()

BASE_URL = config["ris"]["base_url"]

SCRAPE_INTERVAL = config["scraper"]["interval"]
REQUEST_TIMEOUT = config["scraper"]["request_timeout"]

SESSION_RANGE = config["ranges"]["sessions"]

mongo_cfg = config["mongo"]

log.info("Connecting to MongoDB %s:%s", mongo_cfg["host"], mongo_cfg["port"])

client = MongoClient(
    host=mongo_cfg["host"],
    port=mongo_cfg["port"]
)

db = client[mongo_cfg["database"]]

log.info("MongoDB connection ready")


def fetch(url):

    try:

        log.debug("HTTP GET %s", url)

        r = requests.get(url, timeout=REQUEST_TIMEOUT)

        log.debug("HTTP %s -> %s", url, r.status_code)

        if r.status_code == 200:
            return r.text

        log.warning("Non-200 response %s for %s", r.status_code, url)

    except Exception as e:

        log.error("HTTP error for %s: %s", url, e)

    return None


def extract_param(url, name):

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    if name in params:
        return params[name][0]

    return None


def store_session(sid, title, url):

    try:

        db.sessions.update_one(
            {"sid": sid},
            {"$set": {
                "sid": sid,
                "title": title,
                "url": url
            }},
            upsert=True
        )

        log.info("Stored session %s", sid)

    except PyMongoError as e:

        log.error("Mongo error storing session %s: %s", sid, e)


def store_top(tid, sid, title, url):

    try:

        db.tops.update_one(
            {"tid": tid},
            {"$set": {
                "tid": tid,
                "session_id": sid,
                "title": title,
                "url": url
            }},
            upsert=True
        )

        log.info("Stored top %s", tid)

    except PyMongoError as e:

        log.error("Mongo error storing top %s: %s", tid, e)


def store_vorlage(vid, tid, title, url):

    try:

        db.vorlagen.update_one(
            {"vid": vid},
            {"$set": {
                "vid": vid,
                "top_id": tid,
                "title": title,
                "url": url
            }},
            upsert=True
        )

        log.info("Stored vorlage %s", vid)

    except PyMongoError as e:

        log.error("Mongo error storing vorlage %s: %s", vid, e)


def store_document(parent_id, parent_type, title, url):

    try:

        db.documents.update_one(
            {"url": url},
            {"$set": {
                "parent_type": parent_type,
                "parent_id": parent_id,
                "title": title,
                "url": url
            }},
            upsert=True
        )

        log.info("Stored document %s", url)

    except PyMongoError as e:

        log.error("Mongo error storing document %s: %s", url, e)


def parse_documents(soup, parent_id, parent_type):

    for link in soup.select("a[href]"):

        href = link.get("href")

        if href and ".pdf" in href.lower():

            doc_url = urljoin(BASE_URL, href)

            title = link.text.strip()

            log.debug("Found PDF %s", doc_url)

            store_document(parent_id, parent_type, title, doc_url)


def scrape_vorlage(tid, url):

    log.debug("Scraping vorlage %s", url)

    html = fetch(url)

    if not html:
        return

    soup = BeautifulSoup(html, "lxml")

    title = soup.title.string.strip() if soup.title else None

    vid = extract_param(url, "__kvonr")

    if vid:

        store_vorlage(vid, tid, title, url)

        parse_documents(soup, vid, "vorlage")


def scrape_top(sid, url):

    log.debug("Scraping top %s", url)

    html = fetch(url)

    if not html:
        return

    soup = BeautifulSoup(html, "lxml")

    title = soup.title.string.strip() if soup.title else None

    tid = extract_param(url, "__ktonr")

    if tid:

        store_top(tid, sid, title, url)

    for link in soup.select("a[href*='vo0050.asp']"):

        href = link.get("href")

        vorlage_url = urljoin(BASE_URL, href)

        scrape_vorlage(tid, vorlage_url)

    parse_documents(soup, tid, "top")


def scrape_session(sid):

    session_url = f"{BASE_URL}/si0057.asp?__ksinr={sid}"

    log.info("Scraping session %s", session_url)

    html = fetch(session_url)

    if not html:
        log.warning("Session %s not found", sid)
        return

    soup = BeautifulSoup(html, "lxml")

    title = soup.title.string.strip() if soup.title else None

    if not title:
        log.warning("Session %s has no title", sid)
        return

    store_session(sid, title, session_url)

    for link in soup.select("a[href*='to0040.asp']"):

        href = link.get("href")

        top_url = urljoin(BASE_URL, href)

        scrape_top(sid, top_url)


def run():

    start = SESSION_RANGE["start"]
    end = SESSION_RANGE["end"]

    log.info("Starting scraping run: sessions %s-%s", start, end)

    for sid in range(start, end):

        try:

            scrape_session(sid)

        except Exception:

            log.exception("Error scraping session %s", sid)


def main():

    log.info("RIS scraper started")

    while True:

        try:

            run()

        except Exception:

            log.exception("Fatal error during run")

        log.info("Sleeping %s seconds", SCRAPE_INTERVAL)

        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()