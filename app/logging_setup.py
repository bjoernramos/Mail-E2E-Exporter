import logging
import os

DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

logger = logging.getLogger("mail-e2e-exporter")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.handlers.clear()
logger.addHandler(_handler)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
logger.propagate = False
