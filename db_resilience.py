"""
db_resilience.py
-----------------
Makes every Supabase/postgrest call in the bot resilient to transient
connection drops (RemoteProtocolError, ConnectionTerminated, etc.) —
the "Server disconnected" errors seen from get_wallet, algo_quota_guard,
and the spark_office resolver.

postgrest-py already has a `send_with_retry` helper, but it only retries
HTTP 503/520 responses on GET/HEAD requests. It does nothing for a
connection that dies *while the response is being read*, which is what
actually happened (RemoteProtocolError raised inside req.send(), before
there's any response to inspect). That's why it wasn't caught.

This module monkeypatches that function once, at import time, so ALL
postgrest calls anywhere in the bot (sync client) get retried on
transient connection errors — without touching every call site.

Usage: import this once, early, e.g. at the top of database.py:
    import db_resilience  # noqa: F401  (applies the patch on import)
"""

import logging
import time

import httpcore
import httpx

logger = logging.getLogger("db_resilience")

_PATCHED_FLAG = "_resilience_patched"

RETRYABLE_EXCEPTIONS = (
    httpcore.RemoteProtocolError,
    httpcore.ConnectError,
    httpcore.ConnectTimeout,
    httpcore.ReadTimeout,
    httpcore.ReadError,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
    ConnectionError,
)

MAX_ATTEMPTS = 3
BASE_DELAY = 0.5  # seconds, doubled each retry


def _patch_sync():
    import postgrest._sync.request_builder as sync_rb

    if getattr(sync_rb, _PATCHED_FLAG, False):
        return  # already patched, don't double-wrap

    original_send_with_retry = sync_rb.send_with_retry

    def resilient_send_with_retry(req):
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return original_send_with_retry(req)
            except RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    logger.error(
                        "postgrest request failed after %d attempts: %r",
                        MAX_ATTEMPTS, exc,
                    )
                    raise
                delay = BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "postgrest request attempt %d/%d failed (%r), retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, exc, delay,
                )
                time.sleep(delay)
        raise last_exc  # unreachable, keeps type-checkers happy

    sync_rb.send_with_retry = resilient_send_with_retry
    setattr(sync_rb, _PATCHED_FLAG, True)
    logger.info("db_resilience: postgrest sync send_with_retry patched")


def _patch_async():
    """Best-effort: also patch the async builder if the bot uses it anywhere."""
    try:
        import postgrest._async.request_builder as async_rb
    except ImportError:
        return

    if getattr(async_rb, _PATCHED_FLAG, False):
        return

    original_send_with_retry = async_rb.send_with_retry

    async def resilient_send_with_retry(req):
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await original_send_with_retry(req)
            except RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    logger.error(
                        "postgrest async request failed after %d attempts: %r",
                        MAX_ATTEMPTS, exc,
                    )
                    raise
                delay = BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "postgrest async request attempt %d/%d failed (%r), retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, exc, delay,
                )
                import asyncio
                await asyncio.sleep(delay)
        raise last_exc

    async_rb.send_with_retry = resilient_send_with_retry
    setattr(async_rb, _PATCHED_FLAG, True)
    logger.info("db_resilience: postgrest async send_with_retry patched")


_patch_sync()
_patch_async()
