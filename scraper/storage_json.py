import json
import logging
import os
from storage_base import BaseStorage

log = logging.getLogger("ris_scraper.json")

class JSONStorage(BaseStorage):

    def __init__(self, config):

        self.enabled = config.get("enabled", False)
        if self.enabled:
            self.output_dir = config.get("output_dir", "output")
            os.makedirs(self.output_dir, exist_ok=True)
        else:
            self.output_dir = None

    def store_session(self, session_obj):

        if not self.enabled:
            return

        sid = session_obj["sid"]
        path = os.path.join(self.output_dir, f"session_{int(sid):04d}.json")

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(session_obj, f, indent=2, ensure_ascii=False)

            log.info("Session %s written to %s", sid, path)

        except Exception as e:
            log.error("Error writing session %s to JSON: %s", sid, e)

    def store_top(self, tid, sid, title, url):
        if not self.enabled:
            return

    def store_vorlage(self, vid, tid, title, url):
        if not self.enabled:
            return

    def store_document(self, parent_id, parent_type, title, url):
        if not self.enabled:
            return