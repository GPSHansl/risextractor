import logging

log = logging.getLogger("ris_scraper")

storages = []


def set_storages(storages_list):
    global storages
    storages = storages_list
    log.info("Storages set: %s", [type(s).__name__ for s in storages])

class BaseStorage:
    def store_session(self, session_obj): pass
    def store_top(self, tid, sid, title, url): pass
    def store_vorlage(self, vid, tid, title, url): pass
    def store_document(self, parent_id, parent_type, title, url): pass


def store_session_all(session_obj):
    for s in storages:
        if hasattr(s, "store_session"):
            s.store_session(session_obj)


def store_top_all(tid, sid, title, url):
    for s in storages:
        if hasattr(s, "store_top"):
            s.store_top(tid, sid, title, url)


def store_vorlage_all(vid, tid, title, url):
    for s in storages:
        if hasattr(s, "store_vorlage"):
            s.store_vorlage(vid, tid, title, url)


def store_document_all(parent_id, parent_type, title, url):
    for s in storages:
        if hasattr(s, "store_document"):
            s.store_document(parent_id, parent_type, title, url)


def dispatch(method, *args):
    log.debug("Dispatching %s to %d storages", method, len(storages))
    for s in storages:
        fn = getattr(s, method, None)
        if callable(fn):
            log.debug("Calling %s on %s", method, type(s).__name__)
            try:
                fn(*args)
            except Exception:
                log.exception("Storage %s failed in %s", type(s).__name__, method)