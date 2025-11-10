import os
import time
import hashlib
import threading
import logging
import uuid
from typing import Dict, Any, Optional, List

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import yaml
from prometheus_client import CollectorRegistry, Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST
from email.message import EmailMessage
import aiosmtplib
from imapclient import IMAPClient
from imapclient.exceptions import LoginError

# Load .env if present
load_dotenv()

# ---------- Config ----------
DEFAULTS = {
    "exporter": {
        "listen_addr": "0.0.0.0",
        "listen_port": 9782,
        "check_interval_seconds": 300,
        "receive_poll_seconds": 5,
        "receive_timeout_seconds": 120,
        "delete_testmail_after_verify": True,
        "subject_prefix": "[MAIL-E2E]",
        # Prefix for Prometheus metric names. Examples: "mail_" (default), "custom_mail_", or "" for none
        "metrics_prefix": "mail_",
        # Global SMTP timeout as fallback if per-account is not set
        "smtp_timeout_seconds": 60,
        # Optional: probe IMAP briefly on timeout to detect late delivery
        "uncertain_probe_on_timeout": True,
        # Optional: short probe limits
        "uncertain_probe_timeout_seconds": 12,
        "uncertain_probe_poll_seconds": 4,
    },
    "accounts": {},
    "tests": [],
}

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
API_KEY = os.environ.get("API_KEY")
METRICS_USER = os.environ.get("METRICS_USER")
METRICS_PASS = os.environ.get("METRICS_PASS")


class ExporterConfig(BaseModel):
    data: Dict[str, Any]

    @classmethod
    def load(cls, path: str) -> "ExporterConfig":
        data = DEFAULTS.copy()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                # shallow merge for top-level keys
                for k, v in loaded.items():
                    if isinstance(v, dict) and k in data and isinstance(data[k], dict):
                        merged = {**data[k], **v}
                        data[k] = merged
                    else:
                        data[k] = v
        return cls(data=data)

    def tests(self) -> List[Dict[str, Any]]:
        return self.data.get("tests", [])


config = ExporterConfig.load(CONFIG_PATH)
_config_mtime_ns: Optional[int] = None


def _reload_config_if_changed(force: bool = False) -> bool:
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
            new_cfg = ExporterConfig.load(CONFIG_PATH)
            config = new_cfg
            _config_mtime_ns = mtime_ns
            logger.info(f"Config reloaded from {CONFIG_PATH} (mtime_ns={mtime_ns})")
            return True
        except Exception as e:
            logger.error(f"Failed to reload config: {e}")
    return False

# ---------- Metrics ----------
registry = CollectorRegistry()

# Metric name prefix, configurable via exporter.metrics_prefix in config.yaml
METRICS_PREFIX = (config.data.get("exporter", {}).get("metrics_prefix", DEFAULTS["exporter"]["metrics_prefix"])) or ""

# Core E2E metrics (now labeled with route, from, to)
g_send_ok = Gauge(f"{METRICS_PREFIX}send_success", "1 if SMTP send ok else 0", ["route", "from", "to"], registry=registry)

g_recv_ok = Gauge(f"{METRICS_PREFIX}receive_success", "1 if IMAP receive ok else 0", ["route", "from", "to"], registry=registry)

g_roundtrip = Gauge(f"{METRICS_PREFIX}roundtrip_seconds", "Roundtrip seconds from send to receive", ["route", "from", "to"], registry=registry)

g_last_send = Gauge(f"{METRICS_PREFIX}last_send_timestamp", "Unix ts of last send attempt", ["route", "from", "to"], registry=registry)

g_last_recv = Gauge(f"{METRICS_PREFIX}last_receive_timestamp", "Unix ts of last receive", ["route", "from", "to"], registry=registry)

c_errors = Counter(f"{METRICS_PREFIX}test_errors_total", "Errors total labeled by route, from, to and step", ["route", "from", "to", "step"], registry=registry)

g_last_error = Gauge(f"{METRICS_PREFIX}last_error_info", "hash of last error info (label value)", ["route", "from", "to"], registry=registry)

# Build info metric to expose app version in Prometheus
# Labels follow common conventions: version (semver/tag), revision (git sha), build_date (ISO8601)
g_build_info = Gauge(f"{METRICS_PREFIX}build_info", "Build and version information for the exporter", ["version", "revision", "build_date"], registry=registry)

# Exporter config metrics (singletons)
g_cfg_delete = Gauge(f"{METRICS_PREFIX}config_delete_testmail_after_verify", "1 if exporter.delete_testmail_after_verify else 0", [], registry=registry)

g_cfg_receive_timeout = Gauge(f"{METRICS_PREFIX}config_receive_timeout_seconds", "Configured receive timeout seconds", [], registry=registry)

g_cfg_receive_poll = Gauge(f"{METRICS_PREFIX}config_receive_poll_seconds", "Configured receive poll seconds", [], registry=registry)

g_cfg_check_interval = Gauge(f"{METRICS_PREFIX}config_check_interval_seconds", "Configured check interval seconds", [], registry=registry)

# Additional config metric (singleton)
g_cfg_smtp_timeout = Gauge(f"{METRICS_PREFIX}config_smtp_timeout_seconds", "Configured SMTP timeout seconds (effective global or per-cycle)", [], registry=registry)

# Tests mapping info metric (labels expose from/to for each route)
g_test_info = Gauge(f"{METRICS_PREFIX}test_info", "Info metric mapping each test route to from/to accounts (always 1)", ["route", "from", "to"], registry=registry)

# Receive attempt/skip and uncertain send indicators
g_recv_attempted = Gauge(f"{METRICS_PREFIX}receive_attempted", "1 if receive was attempted in the current cycle; else 0", ["route", "from", "to"], registry=registry)

g_recv_skipped = Gauge(f"{METRICS_PREFIX}receive_skipped", "1 if receive was skipped due to send failure; else 0", ["route", "from", "to"], registry=registry)

#gauge indicating send uncertainty (timeout after DATA / late 250)
g_send_uncertain = Gauge(f"{METRICS_PREFIX}send_uncertain", "1 if send result is uncertain (timeout/disconnect likely after DATA)", ["route", "from", "to"], registry=registry)


# ---------- Auth helpers ----------

def require_api_key(request: Request):
    if not API_KEY:
        return  # disabled
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    if key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "invalid api key"})


def require_metrics_basic_auth(request: Request):
    if not (METRICS_USER and METRICS_PASS):
        return  # disabled
    auth = request.headers.get("authorization", "")
    import base64
    if not auth.lower().startswith("basic "):
        raise HTTPException(status_code=401, detail={"error": "basic auth required"}, headers={"WWW-Authenticate": "Basic"})
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        user, pwd = decoded.split(":", 1)
        if not (user == METRICS_USER and pwd == METRICS_PASS):
            raise ValueError("bad creds")
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "invalid credentials"}, headers={"WWW-Authenticate": "Basic"})


# ---------- App ----------
DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")
logger = logging.getLogger("mail-e2e-exporter")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.handlers.clear()
logger.addHandler(_handler)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
logger.propagate = False

# ---------- Version/build metadata ----------
APP_VERSION = os.environ.get("APP_VERSION", "0.2.1")
GIT_SHA = os.environ.get("GIT_SHA", "")
BUILD_DATE = os.environ.get("BUILD_DATE", "")

app = FastAPI(title="Mail E2E Exporter", version=APP_VERSION)


# ---------- Custom exceptions ----------
class SMTPUncertainError(Exception):
    """Raised when SMTP send likely succeeded server-side but client timed out or disconnected (post-DATA).
    We treat this as an uncertain send for optional probing.
    """
    pass

# ---------- Utils ----------

def _expand_env_value(val: Any) -> Any:
    if isinstance(val, str):
        # expand ${VAR} and $VAR
        return os.path.expandvars(val)
    if isinstance(val, dict):
        return {k: _expand_env_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand_env_value(v) for v in val]
    return val


def _get_account(key: str) -> Dict[str, Any]:
    acc = config.data.get("accounts", {}).get(key)
    if not acc:
        raise ValueError(f"account '{key}' not found in config")
    return _expand_env_value(acc)


def _smtp_send(route_name: str, src_key: str, dst_key: str, subject: str, body: str) -> None:
    acc = _get_account(src_key)
    smtp = acc.get("smtp", {})
    host = smtp.get("host")
    port = int(smtp.get("port", 587))
    starttls = bool(smtp.get("starttls", True))
    username = smtp.get("username")
    password = smtp.get("password")
    # Effective SMTP timeout resolution: per-account → global → default
    exporter_cfg = config.data.get("exporter", {}) or {}
    eff_timeout = int(smtp.get("timeout_seconds") or exporter_cfg.get("smtp_timeout_seconds") or DEFAULTS["exporter"]["smtp_timeout_seconds"])

    # Env hint for SMTP password if unresolved
    raw_acc = config.data.get("accounts", {}).get(src_key, {}) or {}
    raw_spw = ((raw_acc.get("smtp", {}) or {}).get("password"))
    smtp_env_hint = None
    if isinstance(raw_spw, str) and "${" in raw_spw:
        try:
            s = raw_spw.index("${") + 2
            e = raw_spw.index("}", s)
            smtp_env_hint = raw_spw[s:e]
        except Exception:
            smtp_env_hint = raw_spw

    dst = _get_account(dst_key)
    to_addr = dst.get("imap", {}).get("username") or dst.get("smtp", {}).get("username")
    if not to_addr:
        raise ValueError("destination username/email missing")

    if not username:
        raise Exception(f"SMTP username missing for account '{src_key}' (host={host})")
    if password is None or password == "":
        msg = f"SMTP password empty for account '{src_key}' user={username} host={host}."
        if smtp_env_hint:
            msg += f" Likely missing env var {smtp_env_hint}."
        raise Exception(msg)

    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    use_tls = not starttls and int(port) == 465

    logger.debug(f"[{route_name}] SMTP connect host={host} port={port} starttls={starttls} use_tls={use_tls}")
    # aiosmtplib is async; run with create_task in a loop would require async context. We use the built-in sync bridge.
    # Use asyncio.run in a short-lived event loop per call to keep background thread simple.
    import asyncio

    async def _send_async(timeout_s: int):
        return await aiosmtplib.send(
            msg,
            hostname=host,
            port=port,
            start_tls=starttls,
            use_tls=use_tls,
            username=username,
            password=password,
            timeout=timeout_s,
        )

    # Retry on timeout-like errors exactly once
    from aiosmtplib import errors as smtp_errors
    attempts = 0
    max_attempts = 2
    last_exc = None
    t0 = time.time()
    while attempts < max_attempts:
        attempts += 1
        try:
            logger.debug(f"[{route_name}] SMTP send attempt {attempts}/{max_attempts} timeout={eff_timeout}s")
            asyncio.run(_send_async(eff_timeout))
            elapsed = time.time() - t0
            logger.debug(f"[{route_name}] SMTP send ok (attempt {attempts}) elapsed={elapsed:.2f}s")
            return
        except (TimeoutError, smtp_errors.SMTPTimeoutError, smtp_errors.SMTPServerDisconnected) as e:  # type: ignore[attr-defined]
            last_exc = e
            if attempts < max_attempts:
                backoff = min(5, max(2, eff_timeout // 20))
                logger.warning(f"[{route_name}] SMTP timeout/disconnect on attempt {attempts}, retrying in {backoff}s... host={host} port={port} starttls={starttls} use_tls={use_tls}")
                time.sleep(backoff)
                continue
            else:
                elapsed = time.time() - t0
                logger.error(f"[{route_name}] SMTP failed after {attempts} attempts due to timeout/disconnect (elapsed={elapsed:.2f}s): {e}")
                # Raise a special error so caller may run an 'uncertain' probe
                raise SMTPUncertainError(str(e))
        except Exception as e:
            # Non-timeout errors: do not retry
            last_exc = e
            logger.error(f"[{route_name}] SMTP send failed (non-timeout) on attempt {attempts}: {e}")
            raise

    # If we get here, re-raise last exception as a safeguard
    if last_exc:
        raise last_exc


def _imap_wait_receive(route_name: str, dst_key: str, subject_token: str, config_exporter: Dict[str, Any]) -> bool:
    acc = _get_account(dst_key)
    imap = acc.get("imap", {})
    host = imap.get("host")
    port = int(imap.get("port", 993))
    ssl_enabled = bool(imap.get("ssl", True))
    username = imap.get("username")
    password = imap.get("password")
    base_folder = imap.get("folder", "INBOX")
    extra_folders_cfg = imap.get("extra_folders", []) or []

    # Try to detect unresolved env var in original config for better hints
    raw_acc = config.data.get("accounts", {}).get(dst_key, {}) or {}
    raw_pw = ((raw_acc.get("imap", {}) or {}).get("password"))
    env_hint = None
    if isinstance(raw_pw, str) and "${" in raw_pw:
        # extract VAR name between ${...}
        try:
            start = raw_pw.index("${") + 2
            end = raw_pw.index("}", start)
            env_hint = raw_pw[start:end]
        except Exception:
            env_hint = raw_pw

    poll = int(config_exporter.get("receive_poll_seconds", DEFAULTS["exporter"]["receive_poll_seconds"]))
    timeout = int(config_exporter.get("receive_timeout_seconds", DEFAULTS["exporter"]["receive_timeout_seconds"]))
    delete_after = bool(config_exporter.get("delete_testmail_after_verify", True))

    # Build folder list. For Gmail, include All Mail/Spam variants to handle label-based filing.
    folders_to_try: List[str] = [base_folder]
    # Normalize configured extra folders to list[str]
    if isinstance(extra_folders_cfg, str):
        extra_folders_cfg = [extra_folders_cfg]
    for f in extra_folders_cfg:
        if f and f not in folders_to_try:
            folders_to_try.append(f)

    is_gmail = (host or "").lower().endswith("gmail.com") or "gmail" in (host or "").lower()
    if is_gmail:
        gmail_candidates = [
            "[Gmail]/All Mail", "[Google Mail]/All Mail",  # EN
            "[Gmail]/Alle Nachrichten", "[Google Mail]/Alle Nachrichten",  # DE
            "[Gmail]/Spam", "[Google Mail]/Spam",
            "[Gmail]/Important", "[Google Mail]/Wichtig",
        ]
        for f in gmail_candidates:
            if f not in folders_to_try:
                folders_to_try.append(f)

    logger.debug(f"[{route_name}] IMAP connect host={host} port={port} ssl={ssl_enabled} folder={base_folder} poll={poll}s timeout={timeout}s")

    if not username:
        raise Exception(f"IMAP username missing for account '{dst_key}' (host={host})")
    if password is None or password == "":
        msg = f"IMAP password empty for account '{dst_key}' user={username} host={host}."
        if env_hint:
            msg += f" Likely missing env var {env_hint}."
        raise Exception(msg)

    deadline = time.time() + timeout
    with IMAPClient(host, port=port, ssl=ssl_enabled) as server:
        try:
            server.login(username, password)
        except LoginError as le:
            hint = ""
            if env_hint:
                hint = f" Hint: set env {env_hint}."
            raise Exception(f"IMAP AUTHENTICATIONFAILED for route='{route_name}' account='{dst_key}' user='{username}' host='{host}' port={port} ssl={ssl_enabled}. {le}.{hint}")
        except Exception as e:
            raise Exception(f"IMAP login failed (route='{route_name}', account='{dst_key}', user='{username}', host='{host}', port={port}, ssl={ssl_enabled}): {e}")

        # Poll across folders until timeout
        while time.time() < deadline:
            for folder in folders_to_try:
                try:
                    server.select_folder(folder)
                except Exception as se:
                    logger.debug(f"[{route_name}] IMAP skipping folder '{folder}': {se}")
                    continue
                try:
                    if is_gmail:
                        # Prefer Gmail's X-GM-RAW to search by subject reliably across locales
                        try:
                            uids = server.gmail_search(f"subject:\"{subject_token}\"")
                        except Exception:
                            # Fallback to regular search if extension not available
                            uids = server.search(["SUBJECT", subject_token], charset="UTF-8")
                    else:
                        uids = server.search(["SUBJECT", subject_token], charset="UTF-8")
                except Exception as se:
                    logger.debug(f"[{route_name}] IMAP search failed in folder '{folder}': {se}")
                    continue

                if uids:
                    logger.debug(f"[{route_name}] IMAP found {len(uids)} message(s) with token {subject_token} in folder '{folder}'")
                    if delete_after:
                        try:
                            server.add_flags(uids, ["\\Deleted"])  # escape backslash
                            server.expunge()
                            logger.debug(f"[{route_name}] IMAP deleted test message(s) in folder '{folder}'")
                        except Exception as de:
                            logger.warning(f"[{route_name}] IMAP delete failed in folder '{folder}': {de}")
                    return True
            time.sleep(poll)
    logger.debug(f"[{route_name}] IMAP receive timeout for token {subject_token}")
    return False


@app.get("/health")
def health(_=Depends(require_api_key)):
    return {"status": "ok", "time": int(time.time())}


@app.get("/info")
def info(_=Depends(require_api_key)):
    try:
        st = os.stat(CONFIG_PATH)
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except FileNotFoundError:
        mtime_ns = None
        size = None
    return {
        "project": "mail-e2e-exporter",
        "version": {
            "app": APP_VERSION,
            "revision": GIT_SHA,
            "build_date": BUILD_DATE,
        },
        "debug": DEBUG,
        "config": {
            "path": CONFIG_PATH,
            "has_config": os.path.exists(CONFIG_PATH),
            "mtime_ns": mtime_ns,
            "size": size,
            "tests": [t.get("name") for t in config.tests()],
        },
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(_=Depends(require_metrics_basic_auth)):
    output = generate_latest(registry)
    return PlainTextResponse(content=output, media_type=CONTENT_TYPE_LATEST)


@app.get("/version")
def version_endpoint(_=Depends(require_api_key)):
    return {"app": APP_VERSION, "revision": GIT_SHA, "build_date": BUILD_DATE}


def _collect_metric_samples(name: str) -> List[Dict[str, Any]]:
    """Collect samples for a given metric name. For Counters, this function
    also tries name+"_total" as the python client may append the suffix.
    Returns list of dicts with keys: labels (dict) and value (float).
    """
    families = list(registry.collect())
    target_names = {name, f"{name}_total"}
    for fam in families:
        if fam.name in target_names:
            out = []
            for s in fam.samples:
                # Skip automatically added *_created samples
                if s.name.endswith("_created"):
                    continue
                out.append({"labels": dict(s.labels), "value": float(s.value)})
            return out
    return []


@app.get("/errors")
def errors_endpoint(_=Depends(require_api_key)):
    """Return a structured JSON view of current error counters and last states,
    grouped by route/from/to. This does not persist history; it reads current
    metric values from the in-process registry.
    """
    # Metric base names (without automatic suffixes)
    name_send_ok = f"{METRICS_PREFIX}send_success"
    name_recv_ok = f"{METRICS_PREFIX}receive_success"
    name_roundtrip = f"{METRICS_PREFIX}roundtrip_seconds"
    name_last_send = f"{METRICS_PREFIX}last_send_timestamp"
    name_last_recv = f"{METRICS_PREFIX}last_receive_timestamp"
    name_errors = f"{METRICS_PREFIX}test_errors_total"
    name_last_error = f"{METRICS_PREFIX}last_error_info"
    name_test_info = f"{METRICS_PREFIX}test_info"

    samples_test_info = _collect_metric_samples(name_test_info)
    samples_send_ok = _collect_metric_samples(name_send_ok)
    samples_recv_ok = _collect_metric_samples(name_recv_ok)
    samples_roundtrip = _collect_metric_samples(name_roundtrip)
    samples_last_send = _collect_metric_samples(name_last_send)
    samples_last_recv = _collect_metric_samples(name_last_recv)
    samples_last_error = _collect_metric_samples(name_last_error)
    samples_errors = _collect_metric_samples(name_errors)

    # Build key set of (route, from, to)
    def key_of(lbls: Dict[str, str]):
        return (lbls.get("route", ""), lbls.get("from", ""), lbls.get("to", ""))

    keys = set()
    for smp in (samples_test_info + samples_send_ok + samples_recv_ok + samples_roundtrip + samples_last_send + samples_last_recv + samples_last_error + samples_errors):
        keys.add(key_of(smp["labels"]))

    # Index helpers
    def index_by_key(samples: List[Dict[str, Any]]):
        d: Dict[tuple, float] = {}
        for s in samples:
            d[key_of(s["labels"])] = s["value"]
        return d

    send_ok_map = index_by_key(samples_send_ok)
    recv_ok_map = index_by_key(samples_recv_ok)
    roundtrip_map = index_by_key(samples_roundtrip)
    last_send_map = index_by_key(samples_last_send)
    last_recv_map = index_by_key(samples_last_recv)
    last_error_map = index_by_key(samples_last_error)

    # Errors need step dimension
    errors_by_key_step: Dict[tuple, Dict[str, float]] = {}
    for s in samples_errors:
        k = key_of(s["labels"])  # route/from/to
        step = s["labels"].get("step", "unknown")
        errors_by_key_step.setdefault(k, {})[step] = s["value"]

    # Assemble response list
    items = []
    for route, frm, to in sorted(keys):
        steps = errors_by_key_step.get((route, frm, to), {})
        items.append({
            "route": route,
            "from": frm,
            "to": to,
            "send_ok": int(send_ok_map.get((route, frm, to), 0)) if not (send_ok_map.get((route, frm, to)) is None) else None,
            "receive_ok": int(recv_ok_map.get((route, frm, to), 0)) if not (recv_ok_map.get((route, frm, to)) is None) else None,
            "roundtrip_seconds": roundtrip_map.get((route, frm, to)),
            "last_send_ts": last_send_map.get((route, frm, to)),
            "last_receive_ts": last_recv_map.get((route, frm, to)),
            "last_error_hash": last_error_map.get((route, frm, to)),
            "errors": {
                "config": steps.get("config", 0),
                "send": steps.get("send", 0),
                "receive": steps.get("receive", 0),
                "other": sum(v for st, v in steps.items() if st not in {"config", "send", "receive"}),
                "total": sum(steps.values()) if steps else 0,
            },
        })

    # Simple summary
    summary = {
        "routes": len(keys),
        "total_errors": sum(it["errors"]["total"] for it in items),
        "timestamp": int(time.time()),
    }

    return JSONResponse({"summary": summary, "items": items})


@app.post("/reload")
def reload_config(_=Depends(require_api_key)):
    reloaded = _reload_config_if_changed(force=True)
    return {
        "reloaded": reloaded,
        "config": {
            "path": CONFIG_PATH,
            "has_config": os.path.exists(CONFIG_PATH),
            "tests": [t.get("name") for t in config.tests()],
        }
    }


# ---------- Background test runner (real flow) ----------
stop_event = threading.Event()


def run_tests_loop():
    while not stop_event.is_set():
        # Hot-reload config if file mtime changed
        if _reload_config_if_changed():
            logger.info("Config change detected; will use new settings for this cycle")
        tests = config.tests()
        exporter_cfg = config.data.get("exporter", {})
        # set config metrics each cycle
        try:
            g_cfg_delete.set(1.0 if bool(exporter_cfg.get("delete_testmail_after_verify", DEFAULTS["exporter"]["delete_testmail_after_verify"])) else 0.0)
            g_cfg_receive_timeout.set(float(exporter_cfg.get("receive_timeout_seconds", DEFAULTS["exporter"]["receive_timeout_seconds"])))
            g_cfg_receive_poll.set(float(exporter_cfg.get("receive_poll_seconds", DEFAULTS["exporter"]["receive_poll_seconds"])))
            g_cfg_check_interval.set(float(exporter_cfg.get("check_interval_seconds", DEFAULTS["exporter"]["check_interval_seconds"])))
            g_cfg_smtp_timeout.set(float(exporter_cfg.get("smtp_timeout_seconds", DEFAULTS["exporter"]["smtp_timeout_seconds"])))
        except Exception:
            pass
        # reset and publish test_info for all tests
        # Note: Prometheus client has no direct reset; we set gauge to 1 for current tests.
        subj_prefix = exporter_cfg.get("subject_prefix", DEFAULTS["exporter"]["subject_prefix"]) or "[MAIL-E2E]"
        if not tests:
            # Expose a default route to show exporter is alive
            route = "no-tests-configured"
            src = "n/a"
            dst = "n/a"
            now = time.time()
            g_test_info.labels(route=route, **{"from": src, "to": dst}).set(1)
            g_send_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
            g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
            g_roundtrip.labels(route=route, **{"from": src, "to": dst}).set(0)
            g_last_send.labels(route=route, **{"from": src, "to": dst}).set(now)
            g_last_recv.labels(route=route, **{"from": src, "to": dst}).set(now)
            # Initialize new gauges
            g_recv_attempted.labels(route=route, **{"from": src, "to": dst}).set(0)
            g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(0)
            g_send_uncertain.labels(route=route, **{"from": src, "to": dst}).set(0)
        else:
            for t in tests:
                src = t.get("from")
                dst = t.get("to")
                route = t.get("name") or f"{src}→{dst}"
                # publish mapping
                g_test_info.labels(route=route, **{"from": src, "to": dst}).set(1)
                # Ensure error-related metrics exist for this route even without errors
                # so that Prometheus scrapes show the series and dashboards don't break.
                c_errors.labels(route=route, **{"from": src, "to": dst}, step="send").inc(0)
                c_errors.labels(route=route, **{"from": src, "to": dst}, step="receive").inc(0)
                g_last_error.labels(route=route, **{"from": src, "to": dst}).set(0)

                if not src or not dst:
                    c_errors.labels(route=route, **{"from": src or "", "to": dst or ""}, step="config").inc()
                    logger.error(f"[{route}] Missing from/to in test config")
                    continue

                # Reset per-cycle receive state to avoid stale values
                g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
                g_roundtrip.labels(route=route, **{"from": src, "to": dst}).set(0)
                g_recv_attempted.labels(route=route, **{"from": src, "to": dst}).set(0)
                g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(0)
                g_send_uncertain.labels(route=route, **{"from": src, "to": dst}).set(0)

                unique = uuid.uuid4().hex[:12]
                subject_token = f"E2E-{unique}"
                subject = f"{subj_prefix} {route} {subject_token}"
                body = f"Mail E2E test for route {route} at {time.strftime('%Y-%m-%d %H:%M:%S %z')} token={subject_token}"

                start = time.time()
                g_last_send.labels(route=route, **{"from": src, "to": dst}).set(start)

                # Determine effective SMTP timeout for config metric visibility (per account/global)
                try:
                    acc_src = _get_account(src)
                    eff_timeout = int((acc_src.get("smtp", {}) or {}).get("timeout_seconds") or exporter_cfg.get("smtp_timeout_seconds") or DEFAULTS["exporter"]["smtp_timeout_seconds"])
                    g_cfg_smtp_timeout.set(float(eff_timeout))
                except Exception:
                    pass

                try:
                    _smtp_send(route, src, dst, subject, body)
                    g_send_ok.labels(route=route, **{"from": src, "to": dst}).set(1)
                    logger.debug(f"[{route}] SMTP send ok to route {dst}")
                except SMTPUncertainError as ue:
                    g_send_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
                    g_send_uncertain.labels(route=route, **{"from": src, "to": dst}).set(1)
                    c_errors.labels(route=route, **{"from": src, "to": dst}, step="send").inc()
                    h = hashlib.md5(str(ue).encode()).hexdigest()
                    g_last_error.labels(route=route, **{"from": src, "to": dst}).set(float(int(h, 16) % 1_000_000))
                    logger.warning(f"[{route}] SMTP uncertain send (timeout/disconnect). Considering short IMAP probe: {ue}")
                    # Optional short probe controlled by exporter flags
                    try:
                        if bool(exporter_cfg.get("uncertain_probe_on_timeout", DEFAULTS["exporter"]["uncertain_probe_on_timeout"])):
                            probe_cfg = dict(exporter_cfg)
                            probe_cfg["receive_timeout_seconds"] = int(exporter_cfg.get("uncertain_probe_timeout_seconds", DEFAULTS["exporter"]["uncertain_probe_timeout_seconds"]))
                            probe_cfg["receive_poll_seconds"] = int(exporter_cfg.get("uncertain_probe_poll_seconds", DEFAULTS["exporter"]["uncertain_probe_poll_seconds"]))
                            g_recv_attempted.labels(route=route, **{"from": src, "to": dst}).set(1)
                            received = _imap_wait_receive(route, dst, subject_token, probe_cfg)
                            if received:
                                endp = time.time()
                                g_last_recv.labels(route=route, **{"from": src, "to": dst}).set(endp)
                                logger.warning(f"[{route}] IMAP probe found message despite SMTP timeout. Marking send_uncertain=1, receive_success remains 1 only for probe visibility.")
                                g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(1)
                            else:
                                g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
                                g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(1)
                        else:
                            g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(1)
                    except Exception as pe:
                        logger.debug(f"[{route}] Probe error or disabled: {pe}")
                        g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(1)
                    continue
                except Exception as e:
                    g_send_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
                    c_errors.labels(route=route, **{"from": src, "to": dst}, step="send").inc()
                    h = hashlib.md5(str(e).encode()).hexdigest()
                    g_last_error.labels(route=route, **{"from": src, "to": dst}).set(float(int(h, 16) % 1_000_000))
                    logger.error(f"[{route}] SMTP error: {e}")
                    # skip receive if send failed
                    g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(1)
                    continue

                # Receive phase
                g_recv_attempted.labels(route=route, **{"from": src, "to": dst}).set(1)
                g_recv_skipped.labels(route=route, **{"from": src, "to": dst}).set(0)
                try:
                    received = _imap_wait_receive(route, dst, subject_token, exporter_cfg)
                    end = time.time()
                    if received:
                        g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(1)
                        g_last_recv.labels(route=route, **{"from": src, "to": dst}).set(end)
                        g_roundtrip.labels(route=route, **{"from": src, "to": dst}).set(end - start)
                        logger.debug(f"[{route}] IMAP receive ok, roundtrip={end - start:.3f}s")
                    else:
                        g_recv_ok.labels(route=route, **{"from": src, "to": dst}).set(0)
                        c_errors.labels(route=route, **{"from": src, "to": dst}, step="receive").inc()
                        g_roundtrip.labels(route=route, **{"from": src, "to": dst}).set(0)
                        logger.warning(f"[{route}] IMAP receive timed out")
                except Exception as e:
                    c_errors.labels(route=route, **{"from": src, "to": dst}, step="receive").inc()
                    h = hashlib.md5(str(e).encode()).hexdigest()
                    g_last_error.labels(route=route, **{"from": src, "to": dst}).set(float(int(h, 16) % 1_000_000))
                    logger.error(f"[{route}] IMAP error: {e}")

        interval = int(exporter_cfg.get("check_interval_seconds", DEFAULTS["exporter"]["check_interval_seconds"]))
        logger.debug(f"Sleep {interval}s until next test cycle")
        stop_event.wait(interval)


@app.on_event("startup")
def on_startup():
    logger.info(f"Starting Mail E2E Exporter v{APP_VERSION} rev={GIT_SHA or 'n/a'} build_date={BUILD_DATE or 'n/a'} DEBUG={DEBUG}")
    # Set build info metric
    try:
        g_build_info.labels(version=APP_VERSION, revision=GIT_SHA or "", build_date=BUILD_DATE or "").set(1)
    except Exception:
        pass
    # Initialize and log config state on startup
    _reload_config_if_changed(force=True)
    t = threading.Thread(target=run_tests_loop, daemon=True)
    t.start()


@app.on_event("shutdown")
def on_shutdown():
    stop_event.set()


# ---------- Error handlers ----------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=detail)


# ---------- Example default config writer (optional) ----------
def ensure_example_config():
    if not os.path.exists(CONFIG_PATH):
        example = {
            "exporter": DEFAULTS["exporter"],
            "accounts": {},
            "tests": [
                {"name": "example-route", "from": "acc1", "to": "acc2"}
            ],
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(example, f, sort_keys=False, allow_unicode=True)
        except Exception:
            pass

# Optionally create example config on first run
if os.environ.get("WRITE_EXAMPLE_CONFIG", "false").lower() in ("1", "true", "yes"):
    ensure_example_config()
