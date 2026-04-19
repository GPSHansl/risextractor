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
from paperless_uploader_new import PaperlessUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("ris_scraper")
config = load_config()

BASE_URL = config["ris"]["base_url"]
SCRAPE_INTERVAL = config["scraper"]["interval"]
REQUEST_TIMEOUT = config["scraper"]["request_timeout"]
DOCUMENTS_OUTPUT_DIR = os.path.join(
    config["storage_json"].get("output_dir", "output"),
    config["scraper"].get("output_dir", "documents")
)
SESSION_RANGE = config["ranges"]["sessions"]

# =========================
# PAPERLESS UPLOADER INITIALISIERUNG
# =========================

try:
    paperless_uploader = PaperlessUploader(config.get("paperless", {}), config["storage_json"].get("output_dir", "output"))
except Exception as e:
    log.error("Error initializing PaperlessUploader: %s", e)

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
                
                # Prefer document with title; only overwrite if new doc has title and old does   
                if doc_id not in docs_dict or (titel and not docs_dict[doc_id].get("titel")):
                    docs_dict[doc_id] = doc_obj
                    log.debug("Found document: %s (id=%s, type=%s)", titel, doc_id, doc_type)
    
    return list(docs_dict.values())


def download_session_documents(session_obj, top_doc_map=None, base_output_dir="documents"):
    """Download all documents for a session and save with metadata files for paperlessNGX.
    Directory structure: documents/YYYYMMDD_SID/
    Files: SID_YYYYMMDD_originalfilename
    """
    counters = {
        "download_ok": 0,
        "download_failed": 0,
        "upload_attempted": 0,
        "upload_success": 0,
        "upload_failed": 0,
        "upload_skipped": 0,
    }

    if "dokumente" not in session_obj or not session_obj["dokumente"]:
        log.debug("No documents to download for session %s", session_obj["sid"])
        return counters
    
    # Parse session date - assuming sidat format is something like "01.04.2026" or similar
    session_date_str = session_obj.get("sidat", "").strip()
    if not session_date_str:
        log.warning("No session date found for session %s, skipping document download", session_obj["sid"])
        return counters
    
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
            return counters
        
        date_yyyymmdd = date_obj.strftime("%Y%m%d")
    except Exception as e:
        log.error("Error parsing session date '%s': %s", session_date_str, e)
        return counters
    
    # Create directory: output/documents/SID_YYYYMMDD/ (SID zero-padded 4-stellig)
    sid = str(session_obj["sid"]).zfill(4)
    session_dir = os.path.join(base_output_dir, f"{sid}_{date_yyyymmdd}")
    os.makedirs(session_dir, exist_ok=True)
    
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
            
            # Prepend SID_YYYYMMDD_[TOP] to filename (SID 4-stellig, TOP 6-stellig)
            name, ext = os.path.splitext(filename)
            top_meta = (top_doc_map or {}).get(doc_id)
            tid_part = ""
            if top_meta and top_meta.get("tid"):
                try:
                    tid_part = f"_{int(top_meta.get('tid')):06d}"
                except (ValueError, TypeError):
                    tid_part = f"_{top_meta.get('tid')}"

            final_filename = f"{sid}_{date_yyyymmdd}{tid_part}_{name}{ext}"
            filepath = os.path.join(session_dir, final_filename)
            
            # Write file
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            log.info("Saved document %s to %s", doc_id, filepath)
            counters["download_ok"] += 1
            
            # Create paperlessNGX metadata JSON file
            metadata = {
                "document_id": doc_id,
                "document_title": doc_title,
                "document_type": doc_type,
                "document_filename": filename,
                "document_url": doc_url,
                "session_id": sid,
                "session_date": date_yyyymmdd,
                "session_title": session_obj.get("title"),
                "session_url": session_obj.get("url_sitzung"),
                "top_list_url": session_obj.get("url_top"),
                "sigrname": session_obj.get("sigrname"),
                "siname": session_obj.get("siname"),
                "heading": session_obj.get("heading")
            }
            
            # Include TOP meta if this document belongs to a TOP
            top_meta = (top_doc_map or {}).get(doc_id)
            if top_meta:
                metadata["top_lfdnr"] = top_meta.get("top_lfdnr")
                metadata["top_titel"] = top_meta.get("top_titel")
                metadata["top_url"] = top_meta.get("top_url")
            tid_part_meta = ""
            if top_meta and top_meta.get("tid"):
                try:
                    tid_part_meta = f"_{int(top_meta.get('tid')):06d}"
                except (ValueError, TypeError):
                    tid_part_meta = f"_{top_meta.get('tid')}"
            metadata_filename = f"{sid}_{date_yyyymmdd}{tid_part_meta}_{name}.json"
            metadata_filepath = os.path.join(session_dir, metadata_filename)
            
            with open(metadata_filepath, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            
            log.debug("Created metadata file %s", metadata_filepath)
            
            # Process document for checksum and Paperless upload
            try:
                upload_result = paperless_uploader.process_document(filepath, metadata)
                if upload_result:
                    counters["upload_attempted"] += upload_result.get("upload_attempted", 0)
                    counters["upload_success"] += upload_result.get("upload_success", 0)
                    counters["upload_failed"] += upload_result.get("upload_failed", 0)
                    counters["upload_skipped"] += upload_result.get("upload_skipped", 0)
            except Exception as e:
                log.error("Error processing document %s: %s", doc_id, e)
                counters["upload_failed"] += 1

        except Exception as e:
            log.error("Error downloading document %s: %s", doc_id, e)
            counters["download_failed"] += 1

    return counters


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

    # Build a map of document ID -> parent TOP metadata for quick lookup
    top_doc_map = {}
    for top in tops:
        if "dokumente" in top:
            for doc_id in top["dokumente"]:
                # keep first occurrence if doc belongs to multiple TOPs
                if doc_id not in top_doc_map:
                    top_doc_map[doc_id] = {
                        "top_lfdnr": top.get("top_lfdnr"),
                        "top_titel": top.get("top_titel"),
                        "top_url": top.get("url"),
                        "tid": top.get("tid")
                    }

    return tops, list(all_top_documents.values()), top_doc_map


def scrape_session(sid):

    session_url = urljoin(BASE_URL, f"si0050.asp?__ksinr={sid}")

    log.info("Scraping Sitzung %s", session_url)

    htmlSession = fetch(session_url, REQUEST_TIMEOUT)

    if not htmlSession:
        log.warning("Session %s not found", sid)
        return None, None

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
        return None, None

    url_top = urljoin(BASE_URL, f"si0057.asp?__ksinr={sid}")
    session_obj["url_top"] = url_top
    tops, top_documents, top_doc_map = get_tops(sid)
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
    counters = download_session_documents(session_obj, top_doc_map, base_output_dir=DOCUMENTS_OUTPUT_DIR)

    log.info(
        "Session %s counters: downloads ok=%d failed=%d | uploads attempted=%d ok=%d failed=%d skipped=%d",
        sid,
        counters["download_ok"],
        counters["download_failed"],
        counters["upload_attempted"],
        counters["upload_success"],
        counters["upload_failed"],
        counters["upload_skipped"],
    )

    return session_obj, counters


# =========================
# RUNNER
# =========================

def run():

    start = SESSION_RANGE["start"]
    end = SESSION_RANGE["end"]

    log.info("Starting scraping run: sessions %s-%s", start, end)

    run_counters = {
        "download_ok": 0,
        "download_failed": 0,
        "upload_attempted": 0,
        "upload_success": 0,
        "upload_failed": 0,
        "upload_skipped": 0,
    }

    for sid in range(start, end):

        try:
            currSession, session_counters = scrape_session(sid)

            if currSession:
                dispatch("store_session", currSession)

            if session_counters:
                run_counters["download_ok"] += session_counters.get("download_ok", 0)
                run_counters["download_failed"] += session_counters.get("download_failed", 0)
                run_counters["upload_attempted"] += session_counters.get("upload_attempted", 0)
                run_counters["upload_success"] += session_counters.get("upload_success", 0)
                run_counters["upload_failed"] += session_counters.get("upload_failed", 0)
                run_counters["upload_skipped"] += session_counters.get("upload_skipped", 0)

        except Exception:
            log.exception("Error scraping session %s", sid)

    log.info(
        "Run counters: downloads ok=%d failed=%d | uploads attempted=%d ok=%d failed=%d skipped=%d",
        run_counters["download_ok"],
        run_counters["download_failed"],
        run_counters["upload_attempted"],
        run_counters["upload_success"],
        run_counters["upload_failed"],
        run_counters["upload_skipped"],
    )

    checksums_ok = 0
    checksums_error = 0
    checksums_total = 0

    for entry in paperless_uploader.checksums.values():
        status = None
        if isinstance(entry, dict):
            status = entry.get("status")
        elif isinstance(entry, str):
            # Legacy checksum format (plain string) counts as successful.
            status = "ok"

        if status == "ok":
            checksums_ok += 1
        else:
            checksums_error += 1

    checksums_total = checksums_ok + checksums_error
    log.info(
        "Checksums counters: total=%d ok=%d error=%d",
        checksums_total,
        checksums_ok,
        checksums_error,
    )


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