import time
from typing import Any, Dict
from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from .logging_setup import logger
from .config import config
from .metrics import g_recv_ok, g_last_recv, g_roundtrip, g_recv_attempted
from .smtp_client import _expand_env_value


def imap_wait_receive(route_name: str, dst_key: str, subject_token: str, config_exporter: Dict[str, Any]) -> Dict[str, Any]:
    dst_raw = config.data.get("accounts", {}).get(dst_key) or {}
    dst = _expand_env_value(dst_raw)
    imap = dst.get("imap", {}) or {}
    host = imap.get("host")
    port = int(imap.get("port", 993))
    use_ssl = bool(imap.get("ssl", True))
    user = imap.get("username")
    pwd = imap.get("password")
    mailbox = imap.get("folder", "INBOX")

    poll_s = int(config_exporter.get("receive_poll_seconds", 5))
    timeout_s = int(config_exporter.get("receive_timeout_seconds", 120))

    start_ts = time.time()
    g_recv_attempted.labels(route=route_name, **{"from": "?", "to": dst_key}).set(1)

    with IMAPClient(host, port=port, ssl=use_ssl, timeout=timeout_s) as client:
        try:
            client.login(user, pwd)
            client.select_folder(mailbox)
        except LoginError as e:
            logger.error(f"IMAP login failed: {e}")
            raise

        # Folders to check: primary mailbox first, then provider-specific fallbacks (e.g., Gmail All Mail/Spam)
        folders = [mailbox]
        host_lc = (host or "").lower()
        if "gmail.com" in host_lc or host_lc.endswith("googlemail.com"):
            # Try common English folder names; localized names may differ, best-effort without listing all mailboxes
            folders += ["[Gmail]/All Mail", "[Gmail]/Spam", "[Google Mail]/All Mail", "[Google Mail]/Spam"]

        # Build search criteria: match in SUBJECT or BODY
        criteria = ['OR', ['SUBJECT', subject_token], ['BODY', subject_token]]

        while True:
            found_msgs = []
            found_folder = None
            for f in folders:
                try:
                    client.select_folder(f)
                except Exception as sel_e:
                    logger.debug(f"[{route_name}] IMAP select folder '{f}' failed: {sel_e}")
                    continue
                try:
                    msgs = client.search(criteria)
                except Exception as se:
                    logger.debug(f"[{route_name}] IMAP search error in '{f}': {se}")
                    msgs = []
                if msgs:
                    found_msgs = msgs
                    found_folder = f
                    break

            if found_msgs:
                g_recv_ok.labels(route=route_name, **{"from": "?", "to": dst_key}).set(1)
                g_last_recv.labels(route=route_name, **{"from": "?", "to": dst_key}).set(time.time())
                g_roundtrip.labels(route=route_name, **{"from": "?", "to": dst_key}).set(time.time() - start_ts)
                if bool(config_exporter.get("delete_testmail_after_verify", True)):
                    try:
                        client.add_flags(found_msgs, ["\\Deleted"])  # escape backslash
                        client.expunge()
                    except Exception as de:
                        logger.debug(f"[{route_name}] delete/expunge failed in '{found_folder}': {de}")
                return {"ok": True, "count": len(found_msgs), "folder": found_folder}

            if (time.time() - start_ts) > timeout_s:
                g_recv_ok.labels(route=route_name, **{"from": "?", "to": dst_key}).set(0)
                return {"ok": False, "timeout": True}

            time.sleep(poll_s)
