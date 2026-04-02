import time
import logging
import os
import json
import re
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests

from url_helpers import fetch, extract_param
from storage_mongo import MongoStorage
from storage_json import JSONStorage
from storage_init import init_storages
from storage_base import dispatch, set_storages
from scraper_config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("ris_scraper")
config = load_config()

BASE_URL = config["ris"]["base_url"]
SCRAPE_INTERVAL = config["scraper"]["interval"]
REQUEST_TIMEOUT = config["scraper"]["request_timeout"]
SESSION_RANGE = config["ranges"]["sessions"]

# =========================
# STORAGE INITIALISIERUNG
# =========================

storages =   init_storages(config)
set_storages(storages)

# =========================
# PARSER
# =========================

def parse_documents(soup):
    """Parse documents (getfile.asp links) from a page and return list of document objects.
    Deduplicates by ID, preferring documents with titles."""
    docs_dict = {}  # Use dict to track by ID and remove duplicates
    
    for link in soup.select("a[href]"):
        href = link.get("href")
        
        if href and "getfile.asp" in href:
            doc_url = urljoin(BASE_URL, href)
            doc_id = extract_param(doc_url, "id")
            doc_type = extract_param(doc_url, "type")
            
            if doc_id:
                titel = link.text.strip()
                doc_obj = {
                    "titel": titel,
                    "id": doc_id,
                    "type": doc_type,
                    "url": doc_url
                }
                
                # Prefer document with title; only overwrite if new doc has title and old doesn't
                if doc_id not in docs_dict or (titel and not docs_dict[doc_id].get("titel")):
                    docs_dict[doc_id] = doc_obj
                    log.debug("Found document: %s (id=%s, type=%s)", titel, doc_id, doc_type)
    
    return list(docs_dict.values())


def download_session_documents(session_obj, base_output_dir="documents"):
    """Download all documents for a session and save with metadata files for paperlessNGX.
    Directory structure: documents/YYYYMMDD_SID/
    Files: SID_YYYYMMDD_originalfilename
    """
    if "dokumente" not in session_obj or not session_obj["dokumente"]:
        log.debug("No documents to download for session %s", session_obj["sid"])
        return
    
    # Parse session date - assuming sidat format is something like "01.04.2026" or similar
    session_date_str = session_obj.get("sidat", "").strip()
    if not session_date_str:
        log.warning("No session date found for session %s, skipping document download", session_obj["sid"])
        return
    
    # Try to parse date and convert to YYYYMMDD format
    try:
        # Try common date formats
        date_obj = None
        for fmt in ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"]:
            try:
                date_obj = datetime.strptime(session_date_str, fmt)
                break
            except ValueError:
                continue
        
        if not date_obj:
            log.warning("Could not parse session date '%s' for session %s", session_date_str, session_obj["sid"])
            return
        
        date_yyyymmdd = date_obj.strftime("%Y%m%d")
    except Exception as e:
        log.error("Error parsing session date '%s': %s", session_date_str, e)
        return
    
    # Create directory: documents/YYYYMMDD_SID/
    session_dir = os.path.join(base_output_dir, f"{date_yyyymmdd}_{session_obj['sid']}")
    os.makedirs(session_dir, exist_ok=True)
    
    sid = session_obj["sid"]
    
    for doc in session_obj["dokumente"]:
        doc_id = doc["id"]
        doc_url = doc.get("url")
        doc_title = doc.get("titel", "")
        doc_type = doc.get("type", "")
        
        if not doc_url:
            log.debug("No URL for document %s, skipping download", doc_id)
            continue
        
        try:
            # Download document
            log.info("Downloading document %s from %s", doc_id, doc_url)
            response = requests.get(doc_url, timeout=REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()
            
            # Get filename from Content-Disposition header or URL
            filename = None
            if "content-disposition" in response.headers:
                try:
                    cd = response.headers.get("content-disposition")
                    filename = re.findall("filename=\"?([^\"]+)\"?", cd)
                    if filename:
                        filename = filename[0]
                except Exception:
                    pass
            
            # Fallback: extract from URL
            if not filename:
                filename = doc_url.split("/")[-1].split("?")[0]
                if not filename or filename == "getfile.asp":
                    filename = f"document_{doc_id}"
            
            # Prepend SID_YYYYMMDD to filename
            name, ext = os.path.splitext(filename)
            final_filename = f"{sid}_{date_yyyymmdd}_{name}{ext}"
            filepath = os.path.join(session_dir, final_filename)
            
            # Write file
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            log.info("Saved document %s to %s", doc_id, filepath)
            
            # Create paperlessNGX metadata JSON file
            metadata = {
                "document_id": doc_id,
                "document_title": doc_title,
                "document_type": doc_type,
                "session_id": sid,
                "session_date": date_yyyymmdd,
                "original_filename": filename,
                "original_url": doc_url
            }
            metadata_filename = f"{sid}_{date_yyyymmdd}_{name}.json"
            metadata_filepath = os.path.join(session_dir, metadata_filename)
            
            with open(metadata_filepath, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            
            log.debug("Created metadata file %s", metadata_filepath)
            
        except Exception as e:
            log.error("Error downloading document %s: %s", doc_id, e)


def scrape_vorlage(tid, url):

    log.debug("Scraping vorlage %s", url)

    html = fetch(url, REQUEST_TIMEOUT)
    if not html:
        return

    soup = BeautifulSoup(html, "lxml")

    title = soup.title.string.strip() if soup.title else None
    vid = extract_param(url, "__kvonr")

    if vid:
        dispatch("store_vorlage", vid, tid, title, url)


def get_tops(sid):

    tops = []
    all_top_documents = {}  # Collect all documents from TOPs
    url_top = urljoin(BASE_URL, f"si0057.asp?__ksinr={sid}")
    log.info("Scraping 'Top Liste' of 'Sitzung' %s (%s)", sid, url_top)

    html = fetch(url_top, REQUEST_TIMEOUT)
    if html:
        soup = BeautifulSoup(html, "lxml")

        trs = soup.select(".smc-t-r-l")
        log.info("Found %d 'Top' elements in session %s", len(trs), sid)
        for top_elem in trs:
            tofnum = top_elem.select_one(".tofnum")
            tolink = top_elem.select_one(".tolink")
            if tofnum and tolink:
                top_lfdnr = tofnum.text.strip()
                top_titel = tolink.text.strip()
                
                top_data = {"sid": sid, "top_lfdnr": top_lfdnr, "top_titel": top_titel}
                
                link = top_elem.select_one("a[href]")
                if link:
                    href = link.get("href")
                    top_kennzeichen = link.text.strip()
                    top_data["top_kennzeichen"] = top_kennzeichen
                    
                    if href and "__kvonr" in href:
                        top_url = urljoin(BASE_URL, href)
                        tid = extract_param(top_url, "__kvonr")
                        top_data["tid"] = tid
                        top_data["url"] = top_url
                        
                        # Parse documents from TOP page
                        html_top = fetch(top_url, REQUEST_TIMEOUT)
                        if html_top:
                            soup_top = BeautifulSoup(html_top, "lxml")
                            doc_list = parse_documents(soup_top)
                            doc_ids = list(dict.fromkeys([doc["id"] for doc in doc_list]))  # Remove duplicates while preserving order
                            if doc_ids:
                                top_data["dokumente"] = doc_ids
                            # Collect documents with full details
                            for doc in doc_list:
                                doc_id = doc["id"]
                                if doc_id not in all_top_documents or (doc.get("titel") and not all_top_documents[doc_id].get("titel")):
                                    all_top_documents[doc_id] = doc
                
                tops.append(top_data)

    return tops, list(all_top_documents.values())


def scrape_session(sid):

    session_url = urljoin(BASE_URL, f"si0050.asp?__ksinr={sid}")

    log.info("Scraping Sitzung %s", session_url)

    htmlSession = fetch(session_url, REQUEST_TIMEOUT)

    if not htmlSession:
        log.warning("Session %s not found", sid)
        return None

    soup = BeautifulSoup(htmlSession, "lxml")

    session_obj = {
        "sid": sid,
        "url_sitzung": session_url,
        "title": soup.title.string.strip() if soup.title else None,
        "heading": soup.select_one("h1").string.strip() if soup.select_one("h1") else None,
        "siname": soup.select_one("div.siname").string.strip() if soup.select_one("div.siname") else None,
        "sigrname": soup.select_one("div.sigrname").string.strip() if soup.select_one("div.sigrname") else None,
        "siort": soup.select_one("div.siort").string.strip() if soup.select_one("div.siort") else None,
        "sidat": soup.select_one("div.sidat").string.strip() if soup.select_one("div.sidat") else None,
        "yytime": soup.select_one("div.yytime").string.strip() if soup.select_one("div.yytime") else None
    }

    log.info("Session %s title: '%s' / '%s'", sid, session_obj["title"], session_obj["heading"])

    if not session_obj["heading"] or session_obj["heading"] == 'SessionNet Fehlermeldung':
        log.warning("Session %s has no valid title", sid)
        return None

    url_top = urljoin(BASE_URL, f"si0057.asp?__ksinr={sid}")
    session_obj["url_top"] = url_top
    tops, top_documents = get_tops(sid)
    session_obj["tops"] = tops
    
    # Parse documents from session page and combine with TOP documents
    session_documents = parse_documents(soup)
    
    # Merge documents: prefer session docs, then add top docs (deduped by ID)
    docs_dict = {}
    for doc in session_documents:
        docs_dict[doc["id"]] = doc
    for doc in top_documents:
        if doc["id"] not in docs_dict or (doc.get("titel") and not docs_dict[doc["id"]].get("titel")):
            docs_dict[doc["id"]] = doc
    
    if docs_dict:
        session_obj["dokumente"] = list(docs_dict.values())

    # Download documents for this session
    download_session_documents(session_obj)

    return session_obj


# =========================
# RUNNER
# =========================

def run():

    start = SESSION_RANGE["start"]
    end = SESSION_RANGE["end"]

    log.info("Starting scraping run: sessions %s-%s", start, end)

    for sid in range(start, end):

        try:
            currSession = scrape_session(sid)

            if currSession:
                dispatch("store_session", currSession)

        except Exception:
            log.exception("Error scraping session %s", sid)


def main():

    log.info("RIS scraper started")

    while True:

        try:
            run()

        except Exception:
            log.exception("Fatal error during run")

        if SCRAPE_INTERVAL < 0:
            log.info("SCRAPE_INTERVAL is negative, exiting after one run")
            break

        log.info("Sleeping %s seconds", SCRAPE_INTERVAL)
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()