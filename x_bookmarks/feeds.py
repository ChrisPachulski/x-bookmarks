"""Frontier sources: feeds, index pages, arXiv, and last30days runs, retrieved
into the same store as X bookmarks.

Retrieval only, like the rest of the project. Each source yields records in
the BookmarkRecord shape with `source` set to `feed:<name>`; the existing
resolve stage turns their links into text and export carries them into
bookmarks.db. Nothing here ranks, summarizes, or decides what matters --
that is the consumer's job (the frontier-scan skill in claude-sync).

Source kinds (feeds.json):
  rss         an RSS 2.0 or Atom URL (GitHub releases feeds are Atom)
  index       a page whose links under `prefix` are the items; for sites
              with no feed (anthropic.com has none)
  page        one page stored again whenever its extracted text changes;
              for changelogs that are a single living document
  arxiv       an arXiv API query, newest first; the link is the PDF so the
              resolve stage captures the full paper
  last30days  a topic run through the last30days engine; every item the
              engine returns lands as its own record
"""

import hashlib
import html
import json
import logging
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import quote, urljoin

from x_bookmarks.records import BookmarkRecord, LinkRecord, classify_link, url_host
from x_bookmarks.resolve import USER_AGENT, read_capped

log = logging.getLogger(__name__)

DEFAULT_SOURCES = Path("feeds.json")
ARXIV_API = "http://export.arxiv.org/api/query"
FETCH_TIMEOUT = 30
# Archive-style feeds return their whole history (openai.com/news serves
# 1,100+ items). The frontier is the recent part; older items are still
# reachable by widening the window in feeds.json.
DEFAULT_SINCE_DAYS = 45
DEFAULT_MAX_ITEMS = 50

###############################################################################
# Types
###############################################################################


class FeedItem(TypedDict):
    """One thing a source produced, before it becomes a record."""

    url: str
    title: str
    summary: str
    published: str    # ISO 8601 UTC, or "" when the source gives none
    author: str
    extra: dict[str, Any]


###############################################################################
# Helpers
###############################################################################


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_id(*parts: str) -> str:
    """Record id for a source item: `feed-` + 16 hex of the joined parts.

    Ids must be filesystem-safe (they name the JSON file) and stable across
    runs so a re-fetch merges into the existing record instead of duplicating
    it. The URL alone is the identity for feed items; page snapshots add the
    content hash so each change is a new record.
    """
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"feed-{digest[:16]}"


def normalize_date(raw: str) -> str:
    """Best-effort ISO UTC from the date formats feeds actually use."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_tags(fragment: str) -> str:
    """Plain text from an HTML fragment (feed summaries are usually HTML)."""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    """GET with the project's browser-ish UA and the resolve stage's size cap."""
    import requests

    with requests.get(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})},
        timeout=FETCH_TIMEOUT, stream=True,
    ) as response:
        response.raise_for_status()
        payload, _truncated = read_capped(response)
    return payload


###############################################################################
# Parsers -- pure, tested against fixtures
###############################################################################

_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def parse_feed(payload: bytes, base_url: str = "") -> list[FeedItem]:
    """Items from an RSS 2.0 or Atom document. Unknown roots yield nothing."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        log.warning("feed at %s is not XML: %s", base_url, exc)
        return []
    items: list[FeedItem] = []
    if root.tag == f"{_ATOM}feed":
        for entry in root.findall(f"{_ATOM}entry"):
            link = ""
            for candidate in entry.findall(f"{_ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = candidate.get("href") or ""
                    break
            summary = _text(entry.find(f"{_ATOM}summary")) or _text(entry.find(f"{_ATOM}content"))
            author = _text(entry.find(f"{_ATOM}author/{_ATOM}name"))
            items.append(FeedItem(
                url=urljoin(base_url, link), title=_text(entry.find(f"{_ATOM}title")),
                summary=strip_tags(summary),
                published=normalize_date(_text(entry.find(f"{_ATOM}published"))
                                         or _text(entry.find(f"{_ATOM}updated"))),
                author=author, extra={},
            ))
        return items
    channel = root.find("channel")
    if channel is None:
        return []
    for item in channel.findall("item"):
        items.append(FeedItem(
            url=urljoin(base_url, _text(item.find("link"))), title=_text(item.find("title")),
            summary=strip_tags(_text(item.find("description"))),
            published=normalize_date(_text(item.find("pubDate"))),
            author=_text(item.find("{http://purl.org/dc/elements/1.1/}creator")) or _text(item.find("author")),
            extra={},
        ))
    return items


def parse_index(payload: bytes, base_url: str, prefix: str) -> list[FeedItem]:
    """Links on a listing page whose path starts with prefix, deduplicated.

    No dates or summaries: an index page rarely carries them reliably, and
    the resolve stage fetches the article itself. The record's created_at is
    therefore the retrieval time, which is honest about what is known.
    """
    text = payload.decode("utf-8", errors="replace")
    seen: set[str] = set()
    items: list[FeedItem] = []
    for match in re.finditer(r'href="([^"#?]+)"', text):
        href = html.unescape(match.group(1))
        url = urljoin(base_url, href)
        path = url.split("//", 1)[-1].split("/", 1)[-1] if "//" in url else url
        if not ("/" + path).startswith(prefix) or ("/" + path).rstrip("/") == prefix.rstrip("/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        items.append(FeedItem(url=url, title=slug.replace("-", " "), summary="",
                              published="", author="", extra={"title_is_slug": True}))
    return items


def parse_arxiv(payload: bytes) -> list[FeedItem]:
    """Entries from an arXiv API Atom response; the link is the PDF."""
    items: list[FeedItem] = []
    for item in parse_feed(payload):
        abs_url = item["url"]
        arxiv_id = abs_url.rstrip("/").rsplit("/", 1)[-1]
        items.append(FeedItem(
            url=f"https://arxiv.org/pdf/{arxiv_id}", title=item["title"],
            summary=item["summary"], published=item["published"], author=item["author"],
            extra={"abs_url": abs_url, "arxiv_id": arxiv_id},
        ))
    return items


def parse_last30days(payload: dict[str, Any], max_items: int) -> list[FeedItem]:
    """Items from the engine's raw JSON, in the engine's ranked order.

    ranked_candidates carries the engine's ordering by item_id; items_by_source
    carries the content. Unranked items follow the ranked ones so nothing the
    engine retrieved is dropped by retrieval.
    """
    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for source, entries in (payload.get("items_by_source") or {}).items():
        for entry in entries or []:
            item_id = str(entry.get("item_id") or entry.get("url") or "")
            if item_id and item_id not in by_id:
                by_id[item_id] = (source, entry)
    ordered: list[str] = []
    for candidate in payload.get("ranked_candidates") or []:
        item_id = str(candidate.get("item_id") or "")
        if item_id in by_id and item_id not in ordered:
            ordered.append(item_id)
    ordered.extend(item_id for item_id in by_id if item_id not in ordered)

    items: list[FeedItem] = []
    for item_id in ordered[:max_items]:
        source, entry = by_id[item_id]
        url = str(entry.get("url") or "")
        if not url:
            continue
        items.append(FeedItem(
            url=url, title=str(entry.get("title") or ""),
            summary=str(entry.get("body") or entry.get("snippet") or ""),
            published=normalize_date(str(entry.get("published_at") or "")),
            author=str(entry.get("author") or ""),
            extra={"engine_source": source, "engagement": entry.get("engagement") or {},
                   "why_relevant": entry.get("why_relevant") or ""},
        ))
    return items


###############################################################################
# Records
###############################################################################


def item_to_record(item: FeedItem, source_name: str, tier: int, retrieved_at: str,
                   record_id: str | None = None, link_text: str = "") -> BookmarkRecord:
    """A BookmarkRecord for a source item, links pending unless text is given."""
    url = item["url"]
    link: LinkRecord = {
        "short_url": url, "expanded_url": url, "display_url": url_host(url),
        "kind": classify_link(url),
        "status": "verified" if link_text else "pending",
        "reason": "" if link_text else "not yet resolved",
        # A slug stands in for the record text but must not occupy the link
        # title, or the resolve stage would keep it over the article's own.
        "title": "" if item["extra"].get("title_is_slug") else item["title"],
        "text": link_text,
    }
    text = item["title"]
    if item["summary"]:
        text = f"{text}\n\n{item['summary']}" if text else item["summary"]
    record: BookmarkRecord = {
        "id": record_id or stable_id(url),
        "text": text,
        "created_at": item["published"] or retrieved_at,
        "lang": "en",
        "author": {"id": source_name, "username": item["author"] or source_name, "name": source_name},
        "links": [link],
        "media": [],
        "metrics": {},
        "referenced": [],
        "article": {"title": "", "text": "", "preview": ""},
        "resolve_status": "verified" if link_text else "pending",
        "retrieved_at": retrieved_at,
        "source": f"feed:{source_name}",
    }
    record["extra"] = {"tier": tier, **item["extra"]}  # type: ignore[typeddict-unknown-key]
    return record


###############################################################################
# Sources
###############################################################################


def load_sources(path: Path = DEFAULT_SOURCES) -> list[dict[str, Any]]:
    """Sources with the file's `defaults` folded into each one."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    defaults = payload.get("defaults") or {}
    return [{**defaults, **s} for s in payload.get("sources", []) if s.get("name") and s.get("kind")]


def within_window(items: list[FeedItem], since_days: int, max_items: int,
                  now: datetime | None = None) -> list[FeedItem]:
    """Newest items inside the window, capped.

    Items with no date (index pages) are kept: the resolve stage learns the
    real date from the article, and dropping them would hide exactly the
    sources that have no feed. Dated items sort newest first.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - since_days * 86400
    kept: list[FeedItem] = []
    for item in items:
        published = item["published"]
        if not published:
            kept.append(item)
            continue
        try:
            stamp = datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
        except ValueError:
            kept.append(item)
            continue
        if stamp >= cutoff:
            kept.append(item)
    kept.sort(key=lambda i: i["published"] or "9", reverse=True)
    return kept[:max_items]


def fetch_rss(source: dict[str, Any]) -> list[FeedItem]:
    return parse_feed(http_get(source["url"]), source["url"])


def fetch_index(source: dict[str, Any]) -> list[FeedItem]:
    return parse_index(http_get(source["url"]), source["url"], source["prefix"])


def fetch_arxiv(source: dict[str, Any]) -> list[FeedItem]:
    url = (f"{ARXIV_API}?search_query={quote(source['query'])}"
           f"&sortBy=submittedDate&sortOrder=descending"
           f"&max_results={int(source.get('max_results', 25))}")
    return parse_arxiv(http_get(url))


def fetch_page(source: dict[str, Any], known_ids: set[str]) -> list[tuple[FeedItem, str, str]]:
    """A single living page: one item per distinct content state.

    Returns (item, record_id, text) so the record is stored already verified;
    the page text is the content, there is no link to resolve later. A page
    whose text hash is already in the store yields nothing.
    """
    from x_bookmarks.resolve import extract_from_html

    payload = http_get(source["url"])
    body = payload.decode("utf-8", errors="replace")
    # Docs sites that render client-side often serve the same page as raw
    # markdown at a .md URL; that body is already text and needs no extraction.
    if source["url"].lower().endswith(".md") or not body.lstrip().startswith("<"):
        text, reason = body.strip(), ""
    else:
        text, reason = extract_from_html(body, source["url"])
    if not text:
        log.warning("page %s yielded no text: %s", source["name"], reason)
        return []
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    record_id = stable_id(source["url"], digest)
    if record_id in known_ids:
        return []
    item = FeedItem(url=source["url"], title=source["name"], summary="",
                    published="", author="", extra={"content_sha1": digest})
    return [(item, record_id, text)]


def fetch_last30days(source: dict[str, Any], engine: Path, python: str,
                     days: int = 30) -> list[FeedItem]:
    """Run the engine headless on a topic and take its raw items.

    Headless means no reasoning model writes the query plan, so the engine's
    deterministic planner runs; that is the documented cron path. The output
    lands in a scratch save-dir so the engine's own library is untouched.
    """
    with tempfile.TemporaryDirectory(prefix="x-bookmarks-l30-") as scratch:
        command = [python, str(engine), source["topic"], "--emit=json",
                   "--json-profile", "raw", "--quick", "--days", str(days),
                   f"--save-dir={scratch}"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=int(source.get("timeout", 900)))
        except subprocess.TimeoutExpired:
            log.warning("last30days timed out on %r", source["topic"])
            return []
        if completed.returncode != 0:
            log.warning("last30days exit %d on %r: %s", completed.returncode,
                        source["topic"], completed.stderr.strip()[-400:])
            return []
        raw_files = sorted(Path(scratch).rglob("*raw.json"), key=lambda p: p.stat().st_mtime)
        if raw_files:
            payload = json.loads(raw_files[-1].read_text(encoding="utf-8"))
        else:
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                log.warning("last30days produced no raw JSON for %r", source["topic"])
                return []
    return parse_last30days(payload, int(source.get("max_items", 30)))


def default_engine() -> Path:
    return Path.home() / "claude-sync" / "skills" / "last30days" / "scripts" / "last30days.py"


###############################################################################
# Driver
###############################################################################


def _record_item(record: BookmarkRecord) -> FeedItem:
    """The FeedItem view of a freshly built record, for windowing."""
    link = record["links"][0]
    return FeedItem(url=link["expanded_url"], title=link["title"], summary="",
                    published=record["created_at"] if record["created_at"] != record["retrieved_at"] else "",
                    author="", extra={})


def fetch_sources(root: Path, sources: list[dict[str, Any]], known_ids: set[str],
                  engine: Path | None = None, python: str = "python3",
                  on_records: Any = None) -> dict[str, dict[str, int]]:
    """Retrieve every source, handing each source's records to on_records.

    Per-source failures are logged and skipped; one dead feed never costs
    the run. Returns per-source counts of items produced.
    """
    retrieved_at = utc_now()
    engine = engine or default_engine()
    counts: dict[str, dict[str, int]] = {}
    for source in sources:
        name, kind, tier = source["name"], source["kind"], int(source.get("tier", 0))
        records: list[BookmarkRecord] = []
        try:
            if kind == "rss":
                records = [item_to_record(i, name, tier, retrieved_at) for i in fetch_rss(source)]
            elif kind == "index":
                records = [item_to_record(i, name, tier, retrieved_at) for i in fetch_index(source)]
            elif kind == "arxiv":
                records = [item_to_record(i, name, tier, retrieved_at) for i in fetch_arxiv(source)]
            elif kind == "page":
                records = [item_to_record(i, name, tier, retrieved_at, record_id=rid, link_text=text)
                           for i, rid, text in fetch_page(source, known_ids)]
            elif kind == "last30days":
                if not engine.exists():
                    log.warning("last30days engine not found at %s; skipping %s", engine, name)
                    counts[name] = {"items": 0, "skipped": 1}
                    continue
                records = [item_to_record(i, name, tier, retrieved_at)
                           for i in fetch_last30days(source, engine, python)]
            else:
                log.warning("unknown source kind %r for %s", kind, name)
                counts[name] = {"items": 0, "skipped": 1}
                continue
        except Exception as exc:  # per-source, non-fatal by design
            log.warning("source %s failed: %s", name, exc)
            counts[name] = {"items": 0, "failed": 1}
            continue
        if kind in ("rss", "index", "arxiv", "last30days"):
            window = within_window([_record_item(r) for r in records],
                                   int(source.get("since_days", DEFAULT_SINCE_DAYS)),
                                   int(source.get("max_items", DEFAULT_MAX_ITEMS)))
            order = {w["url"]: n for n, w in enumerate(window)}
            records = sorted((r for r in records if r["links"][0]["expanded_url"] in order),
                             key=lambda r: order[r["links"][0]["expanded_url"]])
        new = sum(1 for r in records if r["id"] not in known_ids)
        counts[name] = {"items": len(records), "new": new}
        if records and on_records is not None:
            on_records(records)
        log.info("%s: %d items, %d new", name, len(records), new)
    return counts
