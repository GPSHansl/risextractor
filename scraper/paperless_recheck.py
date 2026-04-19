"""
paperless_recheck.py

Cross-checks every document tracked in checksums.json against Paperless-ngx.
For every entry - regardless of checksum status - it queries Paperless for a
document whose custom field 'dokumentenname' starts with the RIS document ID.
If nothing is found the local file is uploaded and the checksum entry is updated to 'ok'.

Before uploading, duplicate checksums (multiple doc IDs sharing the same hash)
are detected and reported in the summary. Duplicates are NOT uploaded.

Usage:
    python paperless_recheck.py [--dry-run] [--filter-status <ok|error|all>]

Options:
    --dry-run           Print what would be uploaded without actually uploading.
    --filter-status     Which checksum-status entries to recheck.
                        'ok'    - only entries that were previously uploaded OK
                        'error' - only entries that failed (default)
                        'all'   - every entry
"""

import argparse
import json
import logging
import os
import sys

import requests

from scraper_config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("paperless_recheck")

HTTP_OK = 200


# ---------------------------------------------------------------------------
# Paperless helpers
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}"}


def fetch_paperless_checksum_cache(base_url: str, token: str) -> dict[str, int]:
    """Page through all Paperless documents once and return a {sha256: paperless_id} mapping.
    Called once at startup so all subsequent existence checks are purely in-memory."""
    cache: dict[str, int] = {}
    url: str | None = f"{base_url.rstrip('/')}/api/documents/"
    params: dict = {"page_size": 100, "ordering": "id"}
    page = 1
    total_fetched = 0

    log.info("Fetching Paperless document index to build checksum cache ...")
    while url:
        try:
            resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
            if resp.status_code != HTTP_OK:
                log.error("Failed to fetch page %d of Paperless documents: HTTP %d - %s",
                          page, resp.status_code, resp.text[:200])
                break
            data = resp.json()
            for doc in data.get("results", []):
                chk = doc.get("checksum")
                pl_id = doc.get("id")
                if chk and pl_id is not None:
                    cache[chk] = int(pl_id)
            total_fetched += len(data.get("results", []))
            url = data.get("next")  # already contains all query params
            params = {}
            page += 1
        except Exception as exc:
            log.error("Error fetching Paperless document page %d: %s", page, exc)
            break

    log.info("Checksum cache built: %d Paperless documents indexed.", total_fetched)
    return cache


def upload_document(base_url: str, token: str, filepath: str, metadata: dict,
                    custom_field_ids: dict) -> bool:
    """Upload a single file to Paperless.  Returns True on success."""
    session_date = metadata.get("session_date", "")
    created = ""
    if session_date and len(session_date) == 8 and session_date.isdigit():
        created = f"{session_date[0:4]}-{session_date[4:6]}-{session_date[6:8]}"

    data = {
        "title": metadata.get("document_title", ""),
        "created": created,
    }

    # Build custom fields dict keyed by Paperless field ID
    custom_fields = {}
    sigrname = metadata.get("sigrname", "") or ""
    siname   = metadata.get("siname", "") or ""
    heading  = metadata.get("heading", "") or ""
    sitzungsname = f"{sigrname} {siname} {heading}".strip()
    if sitzungsname and "sitzungsname" in custom_field_ids:
        custom_fields[str(custom_field_ids["sitzungsname"])] = sitzungsname[:128]

    top_lfdnr = metadata.get("top_lfdnr", "") or ""
    top_titel = metadata.get("top_titel", "") or ""
    top = f"{top_lfdnr} {top_titel}".strip()
    if top and "top" in custom_field_ids:
        custom_fields[str(custom_field_ids["top"])] = top[:128]

    doc_id    = metadata.get("document_id", "") or ""
    doc_title = metadata.get("document_title", "") or ""
    dokumentenname = f"{doc_id} {doc_title}".strip()
    if dokumentenname and "dokumentenname" in custom_field_ids:
        custom_fields[str(custom_field_ids["dokumentenname"])] = dokumentenname[:128]

    if custom_fields:
        data["custom_fields"] = json.dumps(custom_fields)

    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{base_url.rstrip('/')}/api/documents/post_document/",
                headers=_headers(token),
                files={"document": f},
                data=data,
                timeout=60,
            )
        if resp.status_code != HTTP_OK:
            log.error("Upload failed for %s: HTTP %d - %s", filepath, resp.status_code, resp.text[:300])
            return False
        log.info("Uploaded %s", filepath)
        return True
    except Exception as exc:
        log.error("Upload exception for %s: %s", filepath, exc)
        return False


def load_custom_field_ids(base_url: str, token: str) -> dict:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/custom_fields/",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code == HTTP_OK:
            id_map = {field["name"]: field["id"] for field in resp.json().get("results", [])}
            log.info("Custom field IDs: %s", id_map)
            return id_map
        log.error("Could not load custom fields: HTTP %d", resp.status_code)
    except Exception as exc:
        log.error("Error loading custom fields: %s", exc)
    return {}


def verify_paperless_connection(base_url: str, token: str) -> bool:
    """Verify Paperless API reachability and token validity before processing."""
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/documents/",
            headers=_headers(token),
            params={"page_size": 1},
            timeout=15,
        )
        if resp.status_code == HTTP_OK:
            return True
        log.error(
            "Paperless connection check failed: HTTP %d - %s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception as exc:
        log.error("Paperless connection check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, filter_status: str = "error"):
    config = load_config()
    pl_cfg = config.get("paperless", {})

    if not pl_cfg.get("enabled", False):
        log.error("Paperless is not enabled in configuration.  Set paperless.enabled: true.")
        sys.exit(1)

    base_url  = pl_cfg.get("url", "http://paperless:8000")
    token     = pl_cfg.get("token", "")
    base_dir  = config["storage_json"].get("output_dir", "output")
    checksum_file = os.path.join(base_dir, pl_cfg.get("checksum_file", "checksums.json"))
    doc_base_dir  = os.path.join(base_dir, config["scraper"].get("output_dir", "output/documents"))

    if not verify_paperless_connection(base_url, token):
        log.error("Cannot continue recheck because Paperless is unreachable or unauthorized.")
        sys.exit(1)

    if not os.path.exists(checksum_file):
        log.error("checksums.json not found at %s", checksum_file)
        sys.exit(1)

    with open(checksum_file, "r", encoding="utf-8") as fh:
        checksums: dict = json.load(fh)

    log.info("Loaded %d entries from %s", len(checksums), checksum_file)

    # ------------------------------------------------------------------
    # Fetch full Paperless index into memory (no per-doc API calls later)
    # ------------------------------------------------------------------
    paperless_cache = fetch_paperless_checksum_cache(base_url, token)  # {sha256: paperless_id}

    # ------------------------------------------------------------------
    # Duplicate checksum detection
    # ------------------------------------------------------------------
    checksum_to_ids: dict[str, list[str]] = {}
    for doc_id, entry in checksums.items():
        chk = entry.get("checksum") if isinstance(entry, dict) else entry
        if chk:
            checksum_to_ids.setdefault(chk, []).append(doc_id)

    duplicates: dict[str, list[str]] = {
        chk: ids for chk, ids in checksum_to_ids.items() if len(ids) > 1
    }
    duplicate_doc_ids: set[str] = set()
    for ids in duplicates.values():
        duplicate_doc_ids.update(ids)

    if duplicates:
        log.info("Found %d duplicate checksum group(s) affecting %d doc IDs – these will NOT be uploaded.",
                 len(duplicates), len(duplicate_doc_ids))
    else:
        log.info("No duplicate checksums found.")

    # ------------------------------------------------------------------
    # Backfill paperless_id from cache into checksums.json (all entries,
    # including duplicates)
    # ------------------------------------------------------------------
    ids_updated = 0
    for _doc_id, _entry in checksums.items():
        if isinstance(_entry, str):
            _entry = {"checksum": _entry, "status": "ok"}
            checksums[_doc_id] = _entry
        _chk = _entry.get("checksum")
        if _chk and _chk in paperless_cache:
            _pl_id = paperless_cache[_chk]
            if _entry.get("paperless_id") != _pl_id:
                _entry["paperless_id"] = str(_pl_id)
                checksums[_doc_id] = _entry
                ids_updated += 1

    if ids_updated:
        log.info("Backfilled paperless_id for %d entries in checksums.json.", ids_updated)
        with open(checksum_file, "w", encoding="utf-8") as fh:
            json.dump(checksums, fh, indent=2, ensure_ascii=False)
    else:
        log.info("No paperless_id updates needed.")

    custom_field_ids = load_custom_field_ids(base_url, token)
    if not custom_field_ids.get("dokumentenname"):
        log.warning("Custom field 'dokumentenname' was not found in Paperless – uploads will proceed without it.")

    counters = {
        "checked": 0,
        "already_in_paperless": 0,
        "uploaded": 0,
        "upload_failed": 0,
        "local_file_missing": 0,
        "skipped_by_filter": 0,
    }

    for doc_id, entry in checksums.items():
        if isinstance(entry, str):
            entry = {"checksum": entry, "status": "ok"}

        status = entry.get("status", "unknown")

        # Apply filter
        if filter_status != "all":
            if status != filter_status:
                counters["skipped_by_filter"] += 1
                continue

        counters["checked"] += 1

        # Skip duplicates – report only, never upload
        if doc_id in duplicate_doc_ids:
            log.debug("Skipping duplicate doc %s", doc_id)
            continue

        # Resolve local file path
        relative_fp = entry.get("filepath")
        if relative_fp:
            filepath = os.path.join(base_dir, relative_fp)
        else:
            # No filepath stored – we cannot locate the file
            log.warning("No filepath in checksum entry for doc %s, skipping", doc_id)
            counters["local_file_missing"] += 1
            continue

        if not os.path.exists(filepath):
            log.warning("Local file missing for doc %s: %s", doc_id, filepath)
            counters["local_file_missing"] += 1
            continue

        # Check against in-memory cache – no further API calls
        stored_checksum = entry.get("checksum") if isinstance(entry, dict) else None
        if stored_checksum and stored_checksum in paperless_cache:
            log.info("Doc %s already in Paperless (id=%s, cache hit), skipping",
                     doc_id, paperless_cache[stored_checksum])
            counters["already_in_paperless"] += 1
            continue
        if not stored_checksum:
            log.warning("No checksum stored for doc %s, cannot verify – will attempt upload", doc_id)

        if dry_run:
            log.info("[DRY-RUN] Would upload doc %s from %s", doc_id, filepath)
            counters["uploaded"] += 1
            continue

        # Load companion metadata JSON (same name as document but .json extension)
        base_no_ext = os.path.splitext(filepath)[0]
        metadata_path = base_no_ext + ".json"
        metadata = {}
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as mh:
                    metadata = json.load(mh)
            except Exception as exc:
                log.warning("Could not load metadata for doc %s: %s", doc_id, exc)
        else:
            log.warning("No metadata file found for doc %s (%s)", doc_id, metadata_path)
            # Build minimal metadata from checksum entry
            metadata = {"document_id": doc_id}

        success = upload_document(base_url, token, filepath, metadata, custom_field_ids)

        if success:
            entry["status"] = "ok"
            checksums[doc_id] = entry
            counters["uploaded"] += 1
            # Persist updated checksums after each successful upload
            with open(checksum_file, "w", encoding="utf-8") as fh:
                json.dump(checksums, fh, indent=2, ensure_ascii=False)
        else:
            counters["upload_failed"] += 1

    # Final summary
    log.info(
        "Recheck complete | checked=%d already_in_paperless=%d uploaded=%d "
        "upload_failed=%d local_file_missing=%d skipped_by_filter=%d",
        counters["checked"],
        counters["already_in_paperless"],
        counters["uploaded"],
        counters["upload_failed"],
        counters["local_file_missing"],
        counters["skipped_by_filter"],
    )

    if duplicates:
        log.info("--- Duplicate checksums (%d groups) ---", len(duplicates))
        for chk, ids in sorted(duplicates.items()):
            id_list = ", ".join(ids)
            log.info("  checksum=%s  doc_ids=[%s]", chk[:16] + "...", id_list)


def main():
    parser = argparse.ArgumentParser(
        description="Cross-check checksums.json against Paperless and re-upload missing documents."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without actually uploading.",
    )
    parser.add_argument(
        "--filter-status",
        choices=["ok", "error", "all"],
        default="error",
        help="Which checksum-status entries to recheck (default: error).",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, filter_status=args.filter_status)


if __name__ == "__main__":
    main()
