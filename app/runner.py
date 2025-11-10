import asyncio
import threading
import time
import uuid
import hashlib
from typing import Dict, Any

from .logging_setup import logger
from .config import config, APP_VERSION, GIT_SHA, BUILD_DATE
from .metrics import (
    g_test_info, g_cfg_delete, g_cfg_receive_timeout, g_cfg_receive_poll, g_cfg_check_interval,
    g_cfg_smtp_timeout, g_recv_attempted, g_recv_skipped, g_last_error, c_errors, g_build_info,
    g_send_ok, g_recv_ok, g_roundtrip, g_last_send, g_last_recv, g_send_uncertain
)
from .smtp_client import smtp_send, SMTPUncertainError
from .imap_client import imap_wait_receive

_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


def _hash_error(info: Dict[str, Any]) -> int:
    s = str(sorted(info.items())).encode()
    return int(hashlib.md5(s).hexdigest(), 16) % (10**6)


async def _run_one_test(route_name: str, t: Dict[str, Any]):
    exporter_cfg = config.data.get("exporter", {})

    src = t["from"]
    dst = t["to"]
    token = uuid.uuid4().hex[:12]
    subject = f"{exporter_cfg.get('subject_prefix', '[MAIL-E2E]')} {route_name} E2E-{token}"
    body = f"Mail E2E test for route {route_name} token={token}"

    logger.info(f"[{route_name}] starting test from={src} to={dst}")
    g_test_info.labels(route=route_name, **{"from": src, "to": dst}).set(1)

    timeout_s = int(t.get("smtp_timeout_seconds", exporter_cfg.get("smtp_timeout_seconds", 60)))
    g_cfg_smtp_timeout.set(timeout_s)

    g_last_send.labels(route=route_name, **{"from": src, "to": dst}).set(time.time())

    try:
        await smtp_send(route_name, src, dst, subject, body, timeout_s)
        send_ok = True
        logger.info(f"[{route_name}] SMTP send ok")
    except SMTPUncertainError as ue:
        send_ok = False
        g_send_ok.labels(route=route_name, **{"from": src, "to": dst}).set(0)
        g_send_uncertain.labels(route=route_name, **{"from": src, "to": dst}).set(1)
        info = {"route": route_name, "from": src, "to": dst, "step": "send", "error": str(ue)}
        g_last_error.labels(route=route_name, **{"from": src, "to": dst}).set(_hash_error(info))
        c_errors.labels(route=route_name, **{"from": src, "to": dst}, step="send").inc()
        logger.warning(f"[{route_name}] SMTP uncertain send: {ue}")
    except Exception as e:
        send_ok = False
        g_send_ok.labels(route=route_name, **{"from": src, "to": dst}).set(0)
        g_send_uncertain.labels(route=route_name, **{"from": src, "to": dst}).set(0)
        info = {"route": route_name, "from": src, "to": dst, "step": "send", "error": str(e)}
        g_last_error.labels(route=route_name, **{"from": src, "to": dst}).set(_hash_error(info))
        c_errors.labels(route=route_name, **{"from": src, "to": dst}, step="send").inc()
        logger.error(f"[{route_name}] SMTP send failed: {e}")

    if not send_ok:
        g_recv_skipped.labels(route=route_name, **{"from": src, "to": dst}).set(1)
        if exporter_cfg.get("uncertain_probe_on_timeout", True):
            logger.info(f"[{route_name}] probing mailbox due to uncertain/failed send")
            probe_cfg = dict(exporter_cfg)
            res = imap_wait_receive(route_name, dst, token, probe_cfg)
            if res.get("ok"):
                g_recv_ok.labels(route=route_name, **{"from": src, "to": dst}).set(1)
                g_last_recv.labels(route=route_name, **{"from": src, "to": dst}).set(time.time())
                logger.info(f"[{route_name}] probe receive ok (post-uncertain)")
        return

    logger.info(f"[{route_name}] waiting for receive token in mailbox")
    g_recv_attempted.labels(route=route_name, **{"from": src, "to": dst}).set(1)
    res = imap_wait_receive(route_name, dst, token, exporter_cfg)
    if res.get("ok"):
        end = time.time()
        g_recv_ok.labels(route=route_name, **{"from": src, "to": dst}).set(1)
        g_last_recv.labels(route=route_name, **{"from": src, "to": dst}).set(end)
        g_roundtrip.labels(route=route_name, **{"from": src, "to": dst}).set(end - g_last_send.labels(route=route_name, **{"from": src, "to": dst})._value.get())
        folder = res.get("folder") or "(selected)"
        logger.info(f"[{route_name}] receive ok count={res.get('count')} folder={folder}")
    else:
        info = {"route": route_name, "from": src, "to": dst, "step": "receive", "error": "timeout"}
        g_last_error.labels(route=route_name, **{"from": src, "to": dst}).set(_hash_error(info))
        c_errors.labels(route=route_name, **{"from": src, "to": dst}, step="receive").inc()
        logger.warning(f"[{route_name}] receive timeout after {exporter_cfg.get('receive_timeout_seconds', 120)}s")


async def _run_all_tests_once():
    exporter_cfg = config.data.get("exporter", {})
    g_cfg_delete.set(1 if exporter_cfg.get("delete_testmail_after_verify", True) else 0)
    g_cfg_receive_timeout.set(int(exporter_cfg.get("receive_timeout_seconds", 120)))
    g_cfg_receive_poll.set(int(exporter_cfg.get("receive_poll_seconds", 5)))
    g_cfg_check_interval.set(int(exporter_cfg.get("check_interval_seconds", 300)))

    tests = list(config.tests())
    logger.info(f"Starting test cycle: {len(tests)} test(s) configured")

    tasks = []
    for t in tests:
        route_name = t.get("name") or f"{t.get('from')}->{t.get('to')}"
        logger.info(f"[cycle] scheduling route '{route_name}' from={t.get('from')} to={t.get('to')}")
        tasks.append(asyncio.create_task(_run_one_test(route_name, t)))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Test cycle finished")


def _thread_entry():
    # set build_info once per process
    try:
        g_build_info.labels(version=APP_VERSION, revision=GIT_SHA, build_date=BUILD_DATE).set(1)
    except Exception:
        pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    check_interval = int(config.data.get("exporter", {}).get("check_interval_seconds", 300))
    logger.info(f"Background runner loop started (check_interval_seconds={check_interval})")

    while not _stop_event.is_set():
        try:
            loop.run_until_complete(_run_all_tests_once())
        except Exception as e:
            logger.exception(f"test loop failure: {e}")
        finally:
            check_interval = int(config.data.get("exporter", {}).get("check_interval_seconds", 300))
            logger.info(f"Sleeping until next cycle: {check_interval}s")
            _stop_event.wait(check_interval)

    loop.close()


def start_background():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    logger.info(f"Starting Mail E2E Exporter v{APP_VERSION} rev={GIT_SHA or 'n/a'} build_date={BUILD_DATE or 'n/a'}")
    _worker_thread = threading.Thread(target=_thread_entry, name="mail-e2e-runner", daemon=True)
    _worker_thread.start()


def stop_background():
    _stop_event.set()
