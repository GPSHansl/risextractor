import hashlib
import json
import logging
from logging import config
import os
import requests

key_checksum = "checksum"
key_status = "status"

HTTP_OK = 200


log = logging.getLogger("ris_scraper.paperless")

class PaperlessUploader:

    def __init__(self, config, base_dir="output"):
        self.enabled = config.get("enabled", False)
        log.info("Initializing PaperlessUploader with config: %s", config)
        log.info("Base directory for PaperlessUploader: %s", base_dir)
        self.base_dir = base_dir
        if self.enabled:
            self.url = config.get("url", "http://paperless:8000")
            self.token = config.get("token", "")
            checksum_file = config.get("checksum_file", "checksums.json")
            self.checksum_file = os.path.join(base_dir, checksum_file)
            os.makedirs(base_dir, exist_ok=True)
            self.custom_field_ids = self._load_custom_field_ids()
            self.checksums = self.load_checksums()
        else:
            self.url = None
            self.token = None
            self.checksum_file = None
            self.custom_field_ids = {}
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
                        normalized[doc_id] = {key_checksum: entry, key_status: "ok"}
                    else:
                        normalized[doc_id] = {key_checksum: None, key_status: "error"}
                return normalized
            except Exception as e:
                log.error("Error loading checksums: %s", e)
        return {}

    def _load_custom_field_ids(self):
        headers = {
            "Authorization": f"Token {self.token}"
        }
        try:
            response = requests.get(f"{self.url.rstrip('/')}/api/custom_fields/", headers=headers)
            if response.status_code == HTTP_OK:
                fields = response.json()
                id_map = {}
                for field in fields.get("results", []):
                    id_map[field["name"]] = field["id"]
                log.info("Loaded custom field IDs: %s", id_map)
                return id_map
            else:
                log.error("Failed to load custom fields: HTTP %d - %s", response.status_code, response.text)
                return {}
        except Exception as e:
            log.error("Error loading custom fields: %s", e)
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
                    # "correspondent": metadata.get("session_id", ""),
                    # "document_type": metadata.get("document_type", ""),
                    # "tags": metadata.get("session_date", ""),
                    "created": created,
                }

                # Add custom fields
                custom_fields = {}
                sigrname = metadata.get("sigrname", "")
                siname = metadata.get("siname", "")
                heading = metadata.get("heading", "")
                sitzungsname = f"{sigrname} {siname} {heading}".strip()
                if sitzungsname and "sitzungsname" in self.custom_field_ids:
                    custom_fields[str(self.custom_field_ids["sitzungsname"])] = sitzungsname[:128]

                top_lfdnr = metadata.get("top_lfdnr", "")
                top_titel = metadata.get("top_titel", "")
                top = f"{top_lfdnr} {top_titel}".strip()
                if top and "top" in self.custom_field_ids:
                    custom_fields[str(self.custom_field_ids["top"])] = top[:128]

                doc_id = metadata.get("document_id", "")
                doc_title = metadata.get("document_title", "")
                dokumentenname = f"{doc_id} {doc_title}".strip()
                if dokumentenname and "dokumentenname" in self.custom_field_ids:
                    custom_fields[str(self.custom_field_ids["dokumentenname"])] = dokumentenname[:128]

                if custom_fields:
                    data["custom_fields"] = json.dumps(custom_fields)
                response = requests.post(f"{self.url.rstrip('/')}/api/documents/post_document/", headers=headers, files=files, data=data)
                if response.status_code != HTTP_OK:
                    log.error("Error uploading to Paperless: HTTP %d - %s", response.status_code, response.text)
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
        if entry.get(key_status) != "ok":
            return True
        return entry.get(key_checksum) != checksum

    def _mark_uploaded(self, doc_id, checksum, filepath):
        self.checksums[doc_id] = {key_checksum: checksum, key_status: "ok", "filepath": filepath}
        self.save_checksums()

    def _mark_failed(self, doc_id, checksum, error_message, filepath):
        self.checksums[doc_id] = {
            key_checksum: checksum,
            key_status: "error",
            "last_error": error_message,
            "filepath": filepath,
        }
        self.save_checksums()

    def process_document(self, filepath, metadata):
        result = {
            "upload_attempted": 0,
            "upload_success": 0,
            "upload_failed": 0,
            "upload_skipped": 0,
        }

        if not self.enabled:
            return result

        doc_id = metadata.get("document_id")
        if not doc_id:
            log.warning("No document_id in metadata, skipping checksum check")
            return result

        checksum = self.calculate_checksum(filepath)
        if not checksum:
            return result

        relative_filepath = os.path.relpath(filepath, self.base_dir)

        if self._upload_needed(doc_id, checksum):
            result["upload_attempted"] = 1
            entry = self.checksums.get(doc_id)
            old_checksum = entry.get(key_checksum) if isinstance(entry, dict) else entry
            log.info("Document %s upload needed (old checksum=%s, new checksum=%s)", doc_id, old_checksum, checksum)
            success = self.upload_to_paperless(filepath, metadata)
            if success:
                self._mark_uploaded(doc_id, checksum, relative_filepath)
                result["upload_success"] = 1
            else:
                self._mark_failed(doc_id, checksum, "upload failed", relative_filepath)
                result["upload_failed"] = 1
        else:
            log.debug("Document %s unchanged and already uploaded", doc_id)
            result["upload_skipped"] = 1

        return result
