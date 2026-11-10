"""
algo_call_audit.py — who is actually spending the AlgoNode/Nodely quota?

Nodely's free tier counts requests per external IP (200k/day today, 128k/day
and 5 req/s for header-less backend traffic from 1 Jan 2027), and failed
requests count too. algo_quota_guard.record_call() only sees the call sites
that remember to call it, so it can't answer "is it us?".

install() patches aiohttp.ClientSession._request and urllib.request.urlopen
(which algosdk uses) so EVERY outbound request to an AlgoNode/Nodely host is
counted, tagged with the repo file:function that made it. A summary is printed
every 30 minutes while traffic flows and whenever the quota guard trips.

Compare the total here with the provider's number in the 403 body
("Daily free API quota exceeded. 330607 requests"):
  - totals close      -> this bot is the spender; the top callers are the fix list
  - ours far smaller  -> the rest comes from elsewhere on the same egress IP
                         (shared hosting IP, website, other services)

Counts are in-memory, per process, per UTC day. Safe to call install() twice.
"""

import os
import sys
import time
import threading
from collections import Counter
from datetime import datetime, timezone

_HOST_MARKERS = tuple(
    m.strip() for m in os.getenv("ALGO_AUDIT_HOSTS", "algonode.cloud,algonode.dev,nodely.io").split(",") if m.strip()
)
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
PRINT_EVERY_SECONDS = 1800

_lock = threading.Lock()
_counts: Counter = Counter()     # (host, "file.py:function") -> requests
_day = None                      # UTC date the counts belong to
_last_print = 0.0
_installed = False


def _host_of(url: str) -> str | None:
    try:
        host = url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].lower()
    except Exception:
        return None
    return host if any(m in host for m in _HOST_MARKERS) else None


def _caller() -> str:
    """First stack frame that lives in this repo (and isn't this file)."""
    f = sys._getframe(1)
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith(_REPO_DIR) and not fn.endswith("algo_call_audit.py"):
            return f"{os.path.basename(fn)}:{f.f_code.co_name}"
        f = f.f_back
    return "unknown"


def _count(url: str) -> None:
    global _day, _last_print
    host = _host_of(url)
    if not host:
        return
    today = datetime.now(timezone.utc).date()
    key = (host, _caller())
    with _lock:
        if _day != today:
            _counts.clear()
            _day = today
            _last_print = time.monotonic()
        _counts[key] += 1
        due = time.monotonic() - _last_print > PRINT_EVERY_SECONDS
    if due:
        print_summary("periodic")


def summary(top: int = 12) -> str:
    with _lock:
        total = sum(_counts.values())
        rows = _counts.most_common(top)
        day = _day
    if not total:
        return "no AlgoNode/Nodely requests counted yet today"
    lines = [f"{total:,} requests from this process since 00:00 UTC {day}"]
    lines += [f"    {n:>8,}  {host}  {src}" for (host, src), n in rows]
    return "\n".join(lines)


def print_summary(reason: str = "") -> None:
    global _last_print
    _last_print = time.monotonic()
    print(f"[algo_audit] ({reason}) {summary()}")


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    try:
        import aiohttp
        _orig_request = aiohttp.ClientSession._request

        async def _audited_request(self, method, str_or_url, *args, **kwargs):
            try:
                _count(str(str_or_url))
            except Exception:
                pass
            return await _orig_request(self, method, str_or_url, *args, **kwargs)

        aiohttp.ClientSession._request = _audited_request
    except Exception as e:
        print(f"[algo_audit] aiohttp patch skipped: {e}")

    try:
        import urllib.request as _ur
        _orig_urlopen = _ur.urlopen

        def _audited_urlopen(url, *args, **kwargs):
            try:
                _count(getattr(url, "full_url", None) or str(url))
            except Exception:
                pass
            return _orig_urlopen(url, *args, **kwargs)

        _ur.urlopen = _audited_urlopen

        # algosdk does `from urllib.request import urlopen`, so it holds its own
        # reference taken at import time. If it was imported before install()
        # (bot.py's cogs import it first) patching urllib.request alone misses it.
        for modname in ("algosdk.v2client.algod", "algosdk.v2client.indexer"):
            try:
                mod = __import__(modname, fromlist=["urlopen"])
                if getattr(mod, "urlopen", None) is _orig_urlopen:
                    mod.urlopen = _audited_urlopen
            except Exception:
                pass
    except Exception as e:
        print(f"[algo_audit] urllib patch skipped: {e}")

    print(f"[algo_audit] installed — counting requests to {', '.join(_HOST_MARKERS)}")
