import asyncio
import random
import time
from typing import Any, Dict
from email.message import EmailMessage
import aiosmtplib

from .logging_setup import logger
from .config import config
from .metrics import g_send_ok, g_last_send, g_send_uncertain, c_rate_limited


class SMTPUncertainError(Exception):
    """Raised when SMTP send likely succeeded server-side but client timed out/disconnected post-DATA."""
    pass


def _expand_env_value(val: Any):
    import os
    if isinstance(val, str):
        return os.path.expandvars(val)
    if isinstance(val, dict):
        return {k: _expand_env_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_expand_env_value(v) for v in val]
    return val


def _get_account(key: str) -> Dict[str, Any]:
    accs = config.data.get("accounts", {})
    if key not in accs:
        raise ValueError(f"unknown account: {key}")
    return _expand_env_value(accs[key])


async def smtp_send(route_name: str, src_key: str, dst_key: str, subject: str, body: str, timeout_s: int) -> Dict[str, Any]:
    src = _get_account(src_key)
    dst = _get_account(dst_key)

    smtp = src.get("smtp", {}) or {}
    host = smtp.get("host")
    port = int(smtp.get("port", 587))
    starttls = bool(smtp.get("starttls", True))
    use_tls = (not starttls) and int(port) == 465
    username = smtp.get("username")
    password = smtp.get("password")

    to_addr = dst.get("imap", {}).get("username") or (dst.get("smtp", {}) or {}).get("username")
    if not to_addr:
        raise ValueError("destination username/email missing")

    if not username:
        raise Exception(f"SMTP username missing for account '{src_key}' (host={host})")
    if password is None or password == "":
        raise Exception(f"SMTP password empty for account '{src_key}' user={username} host={host}.")

    message = EmailMessage()
    message["From"] = username
    message["To"] = to_addr
    message["Subject"] = subject
    # Force safe textual encoding: UTF-8 + quoted-printable so '=' is encoded as '=3D' and cannot be misinterpreted
    message.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")

    g_last_send.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(time.time())

    from aiosmtplib import errors as smtp_errors

    attempts = 0
    max_attempts = 3
    while attempts < max_attempts:
        attempts += 1
        try:
            await aiosmtplib.send(
                message,
                hostname=host,
                port=port,
                start_tls=starttls,
                use_tls=use_tls,
                username=username,
                password=password,
                timeout=timeout_s,
            )
            g_send_ok.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(1)
            g_send_uncertain.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
            return {"ok": True}
        except smtp_errors.SMTPResponseException as e:
            code = int(getattr(e, "code", 0) or 0)
            if 400 <= code < 500:
                c_rate_limited.labels(route=route_name, **{"from": src_key, "to": dst_key}, code=str(code)).inc()
                if attempts < max_attempts:
                    backoff = min(30, 3 * (2 ** (attempts - 1))) + random.uniform(0, 1.5)
                    logger.warning(f"[{route_name}] SMTP {code} temp failure attempt {attempts}, retrying in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    continue
            g_send_ok.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
            raise
        except (smtp_errors.SMTPTimeoutError, smtp_errors.SMTPServerDisconnected, TimeoutError) as e:  # type: ignore[attr-defined]
            if attempts < max_attempts:
                backoff = min(5, max(2, timeout_s // 20))
                logger.warning(f"[{route_name}] SMTP timeout/disconnect attempt {attempts}, retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                continue
            g_send_ok.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
            g_send_uncertain.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(1)
            raise SMTPUncertainError(str(e))
        except Exception:
            g_send_ok.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
            g_send_uncertain.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
            raise

    # Should not reach here
    g_send_ok.labels(route=route_name, **{"from": src_key, "to": dst_key}).set(0)
    return {"ok": False}
