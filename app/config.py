import os
import yaml
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from dotenv import load_dotenv

# Load .env early
load_dotenv()

DEFAULTS: Dict[str, Any] = {
    "exporter": {
        "listen_addr": "0.0.0.0",
        "listen_port": 9782,
        "check_interval_seconds": 300,
        "receive_poll_seconds": 5,
        "receive_timeout_seconds": 120,
        "delete_testmail_after_verify": True,
        "subject_prefix": "[MAIL-E2E]",
        "metrics_prefix": "mail_",
        "smtp_timeout_seconds": 60,
        "uncertain_probe_on_timeout": True,
        "uncertain_probe_timeout_seconds": 12,
        "uncertain_probe_poll_seconds": 4,
        "min_smtp_interval_seconds": 0,
        "send_jitter_max_seconds": 0,
    },
    "accounts": {},
    "tests": [],
}

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
API_KEY = os.environ.get("API_KEY")
METRICS_USER = os.environ.get("METRICS_USER")
METRICS_PASS = os.environ.get("METRICS_PASS")

APP_VERSION = os.environ.get("APP_VERSION", "0.2.1")
GIT_SHA = os.environ.get("GIT_SHA", "")
BUILD_DATE = os.environ.get("BUILD_DATE", "")


class ExporterConfig(BaseModel):
    data: Dict[str, Any]

    @classmethod
    def load(cls, path: str) -> "ExporterConfig":
        data = DEFAULTS.copy()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in data and isinstance(data[k], dict):
                        data[k] = {**data[k], **v}
                    else:
                        data[k] = v
        return cls(data=data)

    def tests(self) -> List[Dict[str, Any]]:
        return self.data.get("tests", [])


# live config + mtime tracking
config: ExporterConfig = ExporterConfig.load(CONFIG_PATH)
_config_mtime_ns: Optional[int] = None


def reload_config_if_changed(logger, force: bool = False) -> bool:
    global config, _config_mtime_ns
    try:
        st = os.stat(CONFIG_PATH)
        mtime_ns = st.st_mtime_ns
        changed = force or (_config_mtime_ns is None) or (mtime_ns != _config_mtime_ns)
    except FileNotFoundError:
        mtime_ns = None
        changed = force or (_config_mtime_ns is not None)
    if changed:
        try:
            config = ExporterConfig.load(CONFIG_PATH)
            _config_mtime_ns = mtime_ns
            logger.info(f"Config reloaded from {CONFIG_PATH} (mtime_ns={mtime_ns})")
            return True
        except Exception as e:  # keep behavior
            logger.error(f"Failed to reload config: {e}")
    return False
