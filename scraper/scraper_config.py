import logging
import yaml

DEFAULT_CONFIG = "defaults.yml"
RUNTIME_CONFIG = "../config/runtime.yml"

log = logging.getLogger("ris_scraper.config")

def load_config():

    log.info("Loading configuration")

    with open(DEFAULT_CONFIG) as f:
        defaults = yaml.safe_load(f)

    with open(RUNTIME_CONFIG) as f:
        runtime = yaml.safe_load(f)

    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and k in a:
                merge(a[k], v)
            else:
                a[k] = v
        return a

    cfg = merge(defaults, runtime)

    log.info("Configuration loaded")

    return cfg
