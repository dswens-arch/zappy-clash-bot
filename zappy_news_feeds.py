"""
zappy_news_feeds.py
-------------------
Free, keyless outside data for the Zappy Agent Wire:

  * Headlines  — Google News RSS (Technology / Sports topics + an AI search),
                 optional publisher RSS, and the official Hacker News API.
  * Price      — ALGO/USD + 24h change from CoinGecko's public endpoint.

Nothing here needs an API key. Everything is cached so broadcasts read from
memory and a dead source never blocks a post. RSS is parsed with the stdlib
(no feedparser dependency).

Headlines are passed through unchanged (title + source + link). We never copy
article text.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import aiohttp

USER_AGENT = "ZappyNewsBot/1.0 (+https://github.com/dswens-arch/zappy-clash-bot)"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

FEED_TTL_SECONDS  = 20 * 60      # refresh each feed at most every 20 min
PRICE_TTL_SECONDS = 5 * 60
MAX_AGE_HOURS     = 36           # ignore stale headlines
MAX_TITLE_CHARS   = 180
HN_MIN_SCORE      = 150

_GN = "https://news.google.com/rss"
_GN_SUFFIX = "hl=en-US&gl=US&ceid=US:en"

# kind -> list of RSS feed URLs. Add or remove freely; a failing feed is just skipped.
RSS_FEEDS: dict[str, list[str]] = {
    "tech": [
        f"{_GN}/headlines/section/topic/TECHNOLOGY?{_GN_SUFFIX}",
    ],
    "ai": [
        f"{_GN}/search?q=artificial+intelligence+when:1d&{_GN_SUFFIX}",
    ],
    "sports": [
        f"{_GN}/headlines/section/topic/SPORTS?{_GN_SUFFIX}",
        "https://www.espn.com/espn/rss/news",
        "https://feeds.bbci.co.uk/sport/rss.xml",
    ],
}

HN_TOP_URL  = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

PRICE_URL = ("https://api.coingecko.com/api/v3/simple/price"
             "?ids=algorand&vs_currencies=usd&include_24hr_change=true")

# Keep the broadcast light: no tragedies, no politics, no courtroom drama.
_BLOCK = re.compile(
    r"\b(dies|died|dead|death|deaths|killed|kills|killing|murder\w*|shooting|shot dead|stabb\w*|"
    r"assault\w*|rape\w*|abuse\w*|suicide|attack\w*|bomb\w*|terror\w*|war|invasion|hostage\w*|"
    r"massacre|tragedy|tragic|funeral|obituary|overdose|cancer|crash(?:es|ed)?|"
    r"trump|biden|harris|election\w*|congress\w*|senate|senator\w*|republican\w*|democrat\w*|"
    r"gaza|ukraine|israel\w*|hamas|putin|immigration|abortion|police|arrest\w*|"
    r"indict\w*|lawsuit\w*|sentenced|scandal|sexual|racis\w*|"
    r"layoffs?|fired)\b",
    re.IGNORECASE,
)


def is_safe_headline(title: str) -> bool:
    return not _BLOCK.search(title or "")


def story_key(kind: str, link: str) -> str:
    return "hl:" + hashlib.sha1(f"{kind}|{link}".encode()).hexdigest()[:20]


# ── RSS ────────────────────────────────────────────────────────────────────────

def _strip_source_suffix(title: str, source: str | None) -> tuple[str, str | None]:
    """Google News titles look like 'Headline text - Publisher'."""
    if source and title.endswith(f" - {source}"):
        return title[: -(len(source) + 3)].strip(), source
    if " - " in title and not source:
        head, _, tail = title.rpartition(" - ")
        if 2 <= len(tail) <= 40:
            return head.strip(), tail.strip()
    return title.strip(), source


def _parse_rss(xml_text: str, kind: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    channel_title = (root.findtext("./channel/title") or "").strip()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else None
        title, source = _strip_source_suffix(title, source)
        if not source:
            source = channel_title.split(" - ")[0][:40] or None
        published = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                published = parsedate_to_datetime(pub)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        if published and published < cutoff:
            continue
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
        out.append({
            "kind": kind, "title": title, "link": link, "source": source,
            "published": published, "key": story_key(kind, link), "extra": None,
        })
    return out


async def _get_text(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                print(f"[news] {url} -> HTTP {r.status}")
                return None
            return await r.text()
    except Exception as e:
        print(f"[news] fetch failed {url}: {type(e).__name__}: {e}")
        return None


async def _get_json(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                print(f"[news] {url} -> HTTP {r.status}")
                return None
            return await r.json(content_type=None)
    except Exception as e:
        print(f"[news] fetch failed {url}: {type(e).__name__}: {e}")
        return None


async def _fetch_hn(session: aiohttp.ClientSession) -> list[dict]:
    ids = await _get_json(session, HN_TOP_URL)
    if not ids:
        return []
    items = await asyncio.gather(*[_get_json(session, HN_ITEM_URL.format(id=i)) for i in ids[:30]])
    out = []
    for it in items:
        if not it or it.get("type") != "story" or it.get("dead") or it.get("deleted"):
            continue
        if (it.get("score") or 0) < HN_MIN_SCORE:
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        link = it.get("url") or f"https://news.ycombinator.com/item?id={it['id']}"
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1].rstrip() + "…"
        out.append({
            "kind": "tech", "title": title, "link": link, "source": "Hacker News",
            "published": datetime.fromtimestamp(it.get("time", 0), tz=timezone.utc),
            "key": story_key("tech", link),
            "extra": {"score": it.get("score"), "comments": it.get("descendants")},
        })
    return out


# ── Cache ──────────────────────────────────────────────────────────────────────

class NewsFeeds:
    """Per-kind headline cache + ALGO price cache. Safe to call from any task."""

    def __init__(self):
        self._items: dict[str, list[dict]] = {}
        self._fetched_at: dict[str, float] = {}
        self._price: dict | None = None
        self._price_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh_kind(self, kind: str) -> None:
        async with aiohttp.ClientSession() as session:
            tasks = [_get_text(session, u) for u in RSS_FEEDS.get(kind, [])]
            texts = await asyncio.gather(*tasks) if tasks else []
            items: list[dict] = []
            for t in texts:
                if t:
                    items.extend(_parse_rss(t, kind))
            if kind == "tech":
                items.extend(await _fetch_hn(session))
        # de-dupe on title (same story from several feeds)
        seen_titles, unique = set(), []
        for it in items:
            t = re.sub(r"\W+", " ", it["title"].lower()).strip()
            if t in seen_titles:
                continue
            seen_titles.add(t)
            unique.append(it)
        if unique:                       # keep the old cache if a refresh comes back empty
            self._items[kind] = unique
        self._fetched_at[kind] = time.time()

    async def headlines(self, kind: str) -> list[dict]:
        """Safe, recent headlines for 'tech' | 'ai' | 'sports' (newest first)."""
        async with self._lock:
            if time.time() - self._fetched_at.get(kind, 0) > FEED_TTL_SECONDS:
                await self._refresh_kind(kind)
        items = [i for i in self._items.get(kind, []) if is_safe_headline(i["title"])]
        items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items

    async def algo_price(self) -> dict | None:
        """{'usd': float, 'change_24h': float|None} or None if unavailable."""
        if self._price and time.time() - self._price_at < PRICE_TTL_SECONDS:
            return self._price
        async with aiohttp.ClientSession() as session:
            data = await _get_json(session, PRICE_URL)
        try:
            row = data["algorand"]
            self._price = {"usd": float(row["usd"]), "change_24h": row.get("usd_24h_change")}
            self._price_at = time.time()
        except (TypeError, KeyError, ValueError):
            pass                         # fall back to last good value (may be None)
        return self._price
