import logging
from pymongo import MongoClient
from pymongo.errors import PyMongoError

log = logging.getLogger("ris_scraper.mongo")


class MongoStorage:

    def __init__(self, config):

        self.enabled = False

        if not config:
            log.info("MongoDB disabled (no config)")
            return

        try:
            self.client = MongoClient(
                host=config["host"],
                port=config["port"]
            )
            self.db = self.client[config["database"]]
            self.enabled = True

            log.info("MongoDB connection ready")

        except Exception as e:
            log.error("MongoDB connection failed: %s", e)

    def store_session(self, session_obj):

        if not self.enabled:
            return

        try:
            self.db.sessions.update_one(
                {"sid": session_obj["sid"]},
                {"$set": session_obj},
                upsert=True
            )
            log.info("Stored session %s", session_obj["sid"])

        except PyMongoError as e:
            log.error("Mongo error storing session %s: %s", session_obj["sid"], e)

    def store_top(self, tid, sid, title, url):

        if not self.enabled:
            return

        try:
            self.db.tops.update_one(
                {"tid": tid},
                {"$set": {
                    "tid": tid,
                    "session_id": sid,
                    "title": title,
                    "url": url
                }},
                upsert=True
            )

        except PyMongoError as e:
            log.error("Mongo error storing top %s: %s", tid, e)

    def store_vorlage(self, vid, tid, title, url):

        if not self.enabled:
            return

        try:
            self.db.vorlagen.update_one(
                {"vid": vid},
                {"$set": {
                    "vid": vid,
                    "top_id": tid,
                    "title": title,
                    "url": url
                }},
                upsert=True
            )

        except PyMongoError as e:
            log.error("Mongo error storing vorlage %s: %s", vid, e)

    def store_document(self, parent_id, parent_type, title, url):

        if not self.enabled:
            return

        try:
            self.db.documents.update_one(
                {"url": url},
                {"$set": {
                    "parent_type": parent_type,
                    "parent_id": parent_id,
                    "title": title,
                    "url": url
                }},
                upsert=True
            )

        except PyMongoError as e:
            log.error("Mongo error storing document %s: %s", url, e)