# =========================
# STORAGE INITIALISIERUNG
# =========================
import logging
from storage_mongo import MongoStorage
from storage_json import JSONStorage

log = logging.getLogger("ris_scraper.init")


def init_storages(config):

    storages = []

    # MongoDB optional
    mongo_cfg = config.get("storage_mongo")
    if (mongo_cfg and mongo_cfg.get("enabled", False)):
        mongo = MongoStorage(mongo_cfg)
        storages.append(mongo)
        log.info("MongoDB storage initialized")

    # JSON optional
    json_cfg = config.get("storage_json")
    if json_cfg and json_cfg.get("enabled", False):
        json_storage = JSONStorage(json_cfg)
        storages.append(json_storage)
        log.info("JSON storage initialized")

    log.info("Initialized storages: %s", [type(s).__name__ for s in storages])
    
    return storages