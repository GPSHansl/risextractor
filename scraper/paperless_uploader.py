import hashlib
import json
import logging
import os
import requests

log = logging.getLogger("ris_scraper.paperless")

class PaperlessUploader:
    def __init__(self, config, base_dir="output"):
        self.enabled = config.get("enabled", False)
        if self.enabled:
            self.url = config.get("url", None)
            self.token = config.get("token", None)
            checksum_file = config.get("checksum_file", "checksums.json")
            self.checksum_file = os.path.join(base_dir, checksum_file)
            os.makedirs(base_dir, exist_ok=True)
            self.checksums = self.load_checksums()
        else:
            self.url = None
            self.token = None
            self.checksum_file = None
            self.checksums = {}

    def load_checksums(self):
        if self.checksum_file and os.path.exists(self.checksum_file):
            try:
                with open(self.checksum_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                normalized = {}
                for doc_id, entry in raw.items():
                    if isinstance(entry, dict):
                        normalized[doc_id] = entry
                    elif isinstance(entry, str):
                        normalized[doc_id] = {"checksum": entry, "status": "ok"}
                    else:
                        normalized[doc_id] = {"checksum": None, "status": "error"}
                return normalized
            except Exception as e:
                log.error("Error loading checksums: %s", e)
        return {}

    def save_checksums(self):
        if not self.checksum_file:
            return
        try:
            with open(self.checksum_file, "w", encoding="utf-8") as f:
                json.dump(self.checksums, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.error("Error saving checksums: %s", e)

    def calculate_checksum(self, filepath):
        hash_sha256 = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_sha256.update(chunk)
            return hash_sha256.hexdigest()
        except Exception as e:
            log.error("Error calculating checksum for %s: %s", filepath, e)
            return None

    def upload_to_paperless(self, filepath, metadata):
        if not self.enabled:
            return False

        headers = {
            "Authorization": f"Token {self.token}"
        }

        try:
            with open(filepath, "rb") as f:
                files = {"document": f}
                session_date = metadata.get("session_date", "")
                created = ""
                if session_date and len(session_date) == 8 and session_date.isdigit():
                    created = f"{session_date[0:4]}-{session_date[4:6]}-{session_date[6:8]}"

                data = {
                    "title": metadata.get("document_title", ""),
                    "correspondent": metadata.get("session_id", ""),
                    "document_type": metadata.get("document_type", ""),
                    "tags": metadata.get("session_date", ""),
                    "created": created,
                }
                response = requests.post(f"{self.url.rstrip('/')}/api/documents/post_document/", headers=headers, files=files, data=data)
                try:
                    response.raise_for_status()
                except requests.HTTPError as http_err:
                    log.error("Error uploading to Paperless: %s - %s", http_err, response.text)
                    return False
                log.info("Uploaded document %s to Paperless", filepath)
                return True
        except Exception as e:
            log.error("Error uploading to Paperless: %s", e)
            return False

    def _upload_needed(self, doc_id, checksum):
        entry = self.checksums.get(doc_id)
        if not entry:
            return True
        if entry.get("status") != "ok":
            return True
        return entry.get("checksum") != checksum

    def _mark_uploaded(self, doc_id, checksum):
        self.checksums[doc_id] = {"checksum": checksum, "status": "ok"}
        self.save_checksums()

    def _mark_failed(self, doc_id, checksum, error_message):
        self.checksums[doc_id] = {
            "checksum": checksum,
            "status": "error",
            "last_error": error_message,
        }
        self.save_checksums()

    def process_document(self, filepath, metadata):
        if not self.enabled:
            return

        doc_id = metadata.get("document_id")
        if not doc_id:
            log.warning("No document_id in metadata, skipping checksum check")
            return

        checksum = self.calculate_checksum(filepath)
        if not checksum:
            return

        old_checksum = self.checksums.get(doc_id)
        if old_checksum != checksum:
            log.info("Document %s has changed (old: %s, new: %s)", doc_id, old_checksum, checksum)
            self.upload_to_paperless(filepath, metadata)
            self.checksums[doc_id] = checksum
            self.save_checksums()
        else:
            log.debug("Document %s unchanged", doc_id)