from prometheus_client import CollectorRegistry, Gauge, Counter
from .config import config, DEFAULTS

registry = CollectorRegistry()

METRICS_PREFIX = (config.data.get("exporter", {}).get("metrics_prefix", DEFAULTS["exporter"]["metrics_prefix"])) or ""

# Core E2E metrics
g_send_ok = Gauge(f"{METRICS_PREFIX}send_success", "1 if SMTP send ok else 0", ["route", "from", "to"], registry=registry)

g_recv_ok = Gauge(f"{METRICS_PREFIX}receive_success", "1 if IMAP receive ok else 0", ["route", "from", "to"], registry=registry)

g_roundtrip = Gauge(f"{METRICS_PREFIX}roundtrip_seconds", "Roundtrip seconds from send to receive", ["route", "from", "to"], registry=registry)

g_last_send = Gauge(f"{METRICS_PREFIX}last_send_timestamp", "Unix ts of last send attempt", ["route", "from", "to"], registry=registry)

g_last_recv = Gauge(f"{METRICS_PREFIX}last_receive_timestamp", "Unix ts of last receive", ["route", "from", "to"], registry=registry)

c_errors = Counter(f"{METRICS_PREFIX}test_errors_total", "Errors total labeled by route, from, to and step", ["route", "from", "to", "step"], registry=registry)

g_last_error = Gauge(f"{METRICS_PREFIX}last_error_info", "hash of last error info (label value)", ["route", "from", "to"], registry=registry)

# Build info
g_build_info = Gauge(f"{METRICS_PREFIX}build_info", "Build and version information for the exporter", ["version", "revision", "build_date"], registry=registry)

# Config singletons
g_cfg_delete = Gauge(f"{METRICS_PREFIX}config_delete_testmail_after_verify", "1 if exporter.delete_testmail_after_verify else 0", [], registry=registry)

g_cfg_receive_timeout = Gauge(f"{METRICS_PREFIX}config_receive_timeout_seconds", "Configured receive timeout seconds", [], registry=registry)

g_cfg_receive_poll = Gauge(f"{METRICS_PREFIX}config_receive_poll_seconds", "Configured receive poll seconds", [], registry=registry)

g_cfg_check_interval = Gauge(f"{METRICS_PREFIX}config_check_interval_seconds", "Configured check interval seconds", [], registry=registry)

g_cfg_smtp_timeout = Gauge(f"{METRICS_PREFIX}config_smtp_timeout_seconds", "Configured SMTP timeout seconds (effective global or per-cycle)", [], registry=registry)

# Mapping + attempt/skip/uncertain
g_test_info = Gauge(f"{METRICS_PREFIX}test_info", "Info metric mapping each test route to from/to accounts (always 1)", ["route", "from", "to"], registry=registry)

g_recv_attempted = Gauge(f"{METRICS_PREFIX}receive_attempted", "1 if receive was attempted in the current cycle; else 0", ["route", "from", "to"], registry=registry)

g_recv_skipped = Gauge(f"{METRICS_PREFIX}receive_skipped", "1 if receive was skipped due to send failure; else 0", ["route", "from", "to"], registry=registry)

g_send_uncertain = Gauge(f"{METRICS_PREFIX}send_uncertain", "1 if send result is uncertain (timeout/disconnect likely after DATA)", ["route", "from", "to"], registry=registry)

# SMTP temporary failures counter
c_rate_limited = Counter(
    f"{METRICS_PREFIX}send_rate_limited_total",
    "SMTP temporary failures (4xx) during send; labeled by route, from, to, and server reply code",
    ["route", "from", "to", "code"],
    registry=registry,
)
