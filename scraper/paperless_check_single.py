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
log = logging.getLogger("paperless_check_single")

HTTP_OK = 200


def _headers(token: str) -> dict:
    return {"Authorization": f"Token {token}"}


def _query_paperless_by_checksum(base_url: str, token: str, checksum: str) -> dict | None:
    """Query Paperless for a document with an exact SHA256 checksum.
    Returns the first matching document dict, or None."""
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/documents/",
            headers=_headers(token),
            params={"checksum": checksum, "page_size": 1},
            timeout=20,
        )
        if resp.status_code != HTTP_OK:
            log.error("Checksum query failed: HTTP %d - %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        results = data.get("results", [])
        return results[0] if results else None
    except Exception as exc:
        log.error("Checksum query exception: %s", exc)
        return None


def _find_checksum_for_doc_id(checksums_file: str, doc_id: str) -> str | None:
    if not os.path.exists(checksums_file):
        return None
    try:
        with open(checksums_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(str(doc_id))
        if isinstance(entry, dict):
            return entry.get("checksum")
        if isinstance(entry, str):
            return entry
    except Exception as exc:
        log.warning("Could not read checksums file: %s", exc)
    return None


def _query_paperless(base_url: str, token: str, query: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/documents/"
    try:
        resp = requests.get(
            url,
            headers=_headers(token),
            params={"query": query, "page_size": 25},
            timeout=20,
        )
        if resp.status_code != HTTP_OK:
            log.error("Paperless query failed for '%s': HTTP %d - %s", query, resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        return data.get("results", [])
    except Exception as exc:
        log.error("Paperless query exception for '%s': %s", query, exc)
        return []


def _load_custom_field_id(base_url: str, token: str, field_name: str) -> int | None:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/custom_fields/",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code != HTTP_OK:
            log.error("Could not load custom fields: HTTP %d - %s", resp.status_code, resp.text[:200])
            return None

        for field in resp.json().get("results", []):
            if field.get("name") == field_name:
                return int(field.get("id"))
        return None
    except Exception as exc:
        log.error("Error loading custom fields: %s", exc)
        return None


def _query_document_detail(base_url: str, token: str, document_id: int) -> dict | None:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/documents/{document_id}/",
            headers=_headers(token),
            timeout=15,
        )
        if resp.status_code != HTTP_OK:
            return None
        return resp.json()
    except Exception:
        return None


def _extract_custom_field_value(document: dict, target_field_id: int) -> str:
    custom_fields = document.get("custom_fields")
    if isinstance(custom_fields, dict):
        # Some API variants return {'<id>': 'value'}.
        val = custom_fields.get(str(target_field_id))
        if val is None:
            val = custom_fields.get(target_field_id)
        return str(val or "")

    if isinstance(custom_fields, list):
        # Common structure: [{'field': <id>, 'value': '...'}]
        for item in custom_fields:
            if not isinstance(item, dict):
                continue
            if item.get("field") == target_field_id:
                return str(item.get("value") or "")

    return ""


def _matches_by_dokumentenname(document: dict, doc_id: str, target_field_id: int) -> bool:
    value = _extract_custom_field_value(document, target_field_id).strip()
    return value.startswith(f"{doc_id} ") or value == doc_id


def check_document_id(doc_id: str) -> int:
    if not doc_id.strip():
        log.error("--doc-id must not be empty")
        return 2

    cfg = load_config()
    pl_cfg = cfg.get("paperless", {})

    if not pl_cfg.get("enabled", False):
        log.error("Paperless is disabled in config. Set paperless.enabled: true.")
        return 2

    base_url = pl_cfg.get("url", "http://paperless:8000")
    token = pl_cfg.get("token", "")
    base_dir = cfg["storage_json"].get("output_dir", "output")
    checksums_file = os.path.join(base_dir, pl_cfg.get("checksum_file", "checksums.json"))

    # Connectivity/auth check.
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/api/documents/",
            headers=_headers(token),
            params={"page_size": 1},
            timeout=15,
        )
        if resp.status_code != HTTP_OK:
            log.error("Cannot reach Paperless or unauthorized: HTTP %d - %s", resp.status_code, resp.text[:200])
            return 2
    except Exception as exc:
        log.error("Cannot reach Paperless: %s", exc)
        return 2

    dokumentenname_field_id = _load_custom_field_id(base_url, token, "dokumentenname")
    if dokumentenname_field_id is None:
        log.warning("Custom field 'dokumentenname' not found in Paperless – fallback unavailable.")

    # Primary: check by SHA256 checksum if available in local checksums.json
    stored_checksum = _find_checksum_for_doc_id(checksums_file, doc_id)
    if stored_checksum:
        hit = _query_paperless_by_checksum(base_url, token, stored_checksum)
        if hit:
            log.info("FOUND in Paperless (checksum) | doc_id=%s checksum=%s... paperless_id=%s title='%s'",
                     doc_id, stored_checksum[:16], hit.get("id"), hit.get("title"))
            return 0
        log.debug("Not found by checksum for doc %s, trying dokumentenname fallback", doc_id)
    else:
        log.info("No checksum stored locally for doc_id=%s – skipping checksum check", doc_id)

    # Fallback: search by doc_id in custom field 'dokumentenname'
    if dokumentenname_field_id is None:
        log.info("NOT FOUND in Paperless | doc_id=%s", doc_id)
        return 1

    results = _query_paperless(base_url, token, doc_id)
    for doc in results:
        if _matches_by_dokumentenname(doc, doc_id, dokumentenname_field_id):
            log.info(
                "FOUND in Paperless | doc_id=%s id=%s dokumentenname='%s'",
                doc_id,
                doc.get("id"),
                _extract_custom_field_value(doc, dokumentenname_field_id),
            )
            return 0

        # Fallback: list endpoint may omit custom_fields; try full document detail.
        doc_pk = doc.get("id")
        full_doc = None
        if doc_pk is not None:
            try:
                full_doc = _query_document_detail(base_url, token, int(doc_pk))
            except (TypeError, ValueError):
                full_doc = None
        if full_doc and _matches_by_dokumentenname(full_doc, doc_id, dokumentenname_field_id):
            log.info(
                "FOUND in Paperless | doc_id=%s id=%s dokumentenname='%s'",
                doc_id,
                full_doc.get("id"),
                _extract_custom_field_value(full_doc, dokumentenname_field_id),
            )
            return 0

    log.info("NOT FOUND in Paperless | doc_id=%s", doc_id)
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether a RIS document ID exists in Paperless-ngx."
    )
    parser.add_argument("--doc-id", required=True, help="RIS document ID to check.")
    args = parser.parse_args()

    exit_code = check_document_id(args.doc_id)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
