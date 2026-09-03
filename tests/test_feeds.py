"""Feed retrieval tests -- parsers are pure and run on fixtures; the driver
is exercised with fetchers monkeypatched so nothing touches the network.
"""

import json
from pathlib import Path

import pytest

from x_bookmarks import feeds, store

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Blog</title>
<item><title>First post</title><link>https://example.com/a</link>
<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate><dc:creator>Ann</dc:creator>
<description>&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;</description></item>
<item><title>Second</title><link>/relative</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>v1.2.0</title><link rel="alternate" href="https://github.com/o/r/releases/tag/v1.2.0"/>
<updated>2026-09-01T12:00:00Z</updated><author><name>bot</name></author>
<content type="html">&lt;b&gt;notes&lt;/b&gt;</content></entry>
</feed>"""

ARXIV = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2604.25850v1</id><title>Agentic Harness Engineering</title>
<summary>We study harnesses.</summary><published>2026-04-28T00:00:00Z</published>
<author><name>A. Author</name></author>
<link href="http://arxiv.org/abs/2604.25850v1" rel="alternate" type="text/html"/>
<link title="pdf" href="http://arxiv.org/pdf/2604.25850v1" rel="related" type="application/pdf"/>
</entry></feed>"""

INDEX = b"""<html><body>
<a href="/engineering/">Engineering</a>
<a href="/engineering/building-effective-agents">Building effective agents</a>
<a href="/engineering/building-effective-agents?utm=x">dup</a>
<a href="/news/something">news</a>
<a href="https://www.anthropic.com/engineering/writing-tools-for-agents">tools</a>
</body></html>"""


def test_rss_items_carry_title_link_date_author_and_plain_summary():
    items = feeds.parse_feed(RSS, "https://example.com/feed")
    assert items[0]["url"] == "https://example.com/a"
    assert items[0]["title"] == "First post"
    assert items[0]["published"] == "2026-09-01T10:00:00Z"
    assert items[0]["author"] == "Ann"
    assert items[0]["summary"] == "Hello & welcome"


def test_rss_relative_link_resolves_against_feed_url():
    items = feeds.parse_feed(RSS, "https://example.com/feed")
    assert items[1]["url"] == "https://example.com/relative"
    assert items[1]["published"] == ""


def test_atom_entries_use_alternate_link_and_updated_date():
    items = feeds.parse_feed(ATOM)
    assert items[0]["url"] == "https://github.com/o/r/releases/tag/v1.2.0"
    assert items[0]["published"] == "2026-09-01T12:00:00Z"
    assert items[0]["summary"] == "notes"


def test_non_xml_payload_yields_no_items():
    assert feeds.parse_feed(b"<!DOCTYPE html><html></html>", "https://x") == []


def test_arxiv_link_is_the_pdf_and_abs_url_is_kept():
    items = feeds.parse_arxiv(ARXIV)
    assert items[0]["url"] == "https://arxiv.org/pdf/2604.25850v1"
    assert items[0]["extra"]["abs_url"] == "http://arxiv.org/abs/2604.25850v1"
    assert items[0]["summary"] == "We study harnesses."


def test_index_keeps_only_links_under_prefix_deduplicated():
    items = feeds.parse_index(INDEX, "https://www.anthropic.com/engineering", "/engineering/")
    urls = [i["url"] for i in items]
    assert urls == [
        "https://www.anthropic.com/engineering/building-effective-agents",
        "https://www.anthropic.com/engineering/writing-tools-for-agents",
    ]
    assert items[0]["title"] == "building effective agents"


def test_last30days_items_follow_engine_ranking_then_the_rest():
    payload = {
        "items_by_source": {
            "reddit": [{"item_id": "R1", "url": "https://r/1", "title": "one", "body": "b1"}],
            "hackernews": [{"item_id": "H1", "url": "https://h/1", "title": "two", "snippet": "s2",
                            "published_at": "2026-09-01T00:00:00Z"}],
            "x": [{"item_id": "X1", "url": "", "title": "no url"}],
        },
        "ranked_candidates": [{"item_id": "H1"}],
    }
    items = feeds.parse_last30days(payload, max_items=10)
    assert [i["url"] for i in items] == ["https://h/1", "https://r/1"]
    assert items[0]["summary"] == "s2"
    assert items[0]["extra"]["engine_source"] == "hackernews"


def test_record_id_is_stable_for_the_same_url():
    item = feeds.FeedItem(url="https://e/a", title="t", summary="", published="", author="", extra={})
    a = feeds.item_to_record(item, "src", 1, "2026-09-03T00:00:00Z")
    b = feeds.item_to_record(item, "src", 1, "2026-09-04T00:00:00Z")
    assert a["id"] == b["id"] and a["id"].startswith("feed-")
    assert a["source"] == "feed:src"
    assert a["links"][0]["status"] == "pending"
    assert a["resolve_status"] == "pending"
    assert a["created_at"] == "2026-09-03T00:00:00Z"  # no published date: retrieval time


def test_index_items_leave_the_link_title_for_the_article_to_fill():
    item = feeds.parse_index(INDEX, "https://www.anthropic.com/engineering", "/engineering/")[0]
    record = feeds.item_to_record(item, "src", 1, "2026-09-03T00:00:00Z")
    assert record["links"][0]["title"] == ""
    assert record["text"] == "building effective agents"


def test_page_source_stores_markdown_bodies_without_html_extraction(monkeypatch):
    monkeypatch.setattr(feeds, "http_get", lambda url, headers=None: b"---\ntitle: notes\n---\n## 2026-09-01\nAdded X.")
    got = feeds.fetch_page({"name": "n", "url": "https://docs/notes.md"}, set())
    assert got[0][2].startswith("---\ntitle: notes") and "Added X." in got[0][2]


def test_page_snapshot_record_is_verified_with_its_text():
    item = feeds.FeedItem(url="https://e/notes", title="notes", summary="", published="", author="", extra={})
    record = feeds.item_to_record(item, "notes", 1, "2026-09-03T00:00:00Z",
                                  record_id="feed-abc", link_text="the page text")
    assert record["id"] == "feed-abc"
    assert record["resolve_status"] == "verified"
    assert record["links"][0]["text"] == "the page text"


def test_driver_persists_records_and_survives_a_failing_source(tmp_path, monkeypatch):
    def ok(source):
        return [feeds.FeedItem(url="https://e/a", title="t", summary="", published="", author="", extra={})]

    def boom(source):
        raise RuntimeError("feed is down")

    monkeypatch.setattr(feeds, "fetch_rss", ok)
    monkeypatch.setattr(feeds, "fetch_index", boom)
    root = store.ensure_store(tmp_path / "store")
    saved: list = []
    counts = feeds.fetch_sources(
        root,
        [{"name": "good", "kind": "rss", "url": "u"}, {"name": "bad", "kind": "index", "url": "u", "prefix": "/"}],
        set(), on_records=lambda records: saved.extend(records),
    )
    assert counts["good"] == {"items": 1, "new": 1}
    assert counts["bad"] == {"items": 0, "failed": 1}
    assert saved[0]["source"] == "feed:good"


def test_page_source_skips_an_unchanged_snapshot(monkeypatch):
    monkeypatch.setattr(feeds, "http_get", lambda url, headers=None: b"<html><body><p>Same text here, long enough to extract as a paragraph of notes.</p></body></html>")
    from x_bookmarks import resolve
    monkeypatch.setattr(resolve, "extract_from_html", lambda html, url: ("page text", ""))
    first = feeds.fetch_page({"name": "n", "url": "https://e/notes"}, set())
    assert len(first) == 1
    _item, record_id, text = first[0]
    assert text == "page text"
    again = feeds.fetch_page({"name": "n", "url": "https://e/notes"}, {record_id})
    assert again == []


def test_last30days_source_is_skipped_when_engine_is_missing(tmp_path):
    counts = feeds.fetch_sources(
        tmp_path, [{"name": "l", "kind": "last30days", "topic": "t"}], set(),
        engine=tmp_path / "missing.py", on_records=lambda r: None,
    )
    assert counts["l"] == {"items": 0, "skipped": 1}


def test_feed_records_round_trip_through_export(tmp_path):
    from x_bookmarks import export
    root = store.ensure_store(tmp_path / "store")
    item = feeds.FeedItem(url="https://e/a", title="t", summary="s", published="", author="", extra={"k": 1})
    store.save_records(root, [feeds.item_to_record(item, "src", 2, "2026-09-03T00:00:00Z")])
    db = tmp_path / "b.db"
    counts = export.export_db(root, db)
    assert counts == {"bookmarks": 1, "links": 1}
    records, skipped = export.read_db_records(db)
    assert skipped == 0 and records[0]["source"] == "feed:src" and records[0]["extra"]["tier"] == 2


def test_window_keeps_recent_dated_and_all_undated_items_newest_first():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    mk = lambda url, published: feeds.FeedItem(url=url, title="", summary="", published=published, author="", extra={})
    items = [mk("old", "2026-01-01T00:00:00Z"), mk("undated", ""), mk("new", "2026-09-01T00:00:00Z"),
             mk("newer", "2026-09-02T00:00:00Z")]
    kept = feeds.within_window(items, since_days=45, max_items=2, now=now)
    assert [i["url"] for i in kept] == ["undated", "newer"]


def test_driver_applies_the_window_to_archive_feeds(tmp_path, monkeypatch):
    def archive(source):
        return [feeds.FeedItem(url=f"https://e/{i}", title="", summary="",
                               published=f"20{i:02d}-01-01T00:00:00Z", author="", extra={}) for i in range(10, 27)]
    monkeypatch.setattr(feeds, "fetch_rss", archive)
    saved: list = []
    feeds.fetch_sources(tmp_path, [{"name": "a", "kind": "rss", "url": "u", "since_days": 36500, "max_items": 3}],
                        set(), on_records=lambda r: saved.extend(r))
    assert [r["links"][0]["expanded_url"] for r in saved] == ["https://e/26", "https://e/25", "https://e/24"]


def test_load_sources_folds_defaults_into_each_source(tmp_path):
    path = tmp_path / "feeds.json"
    path.write_text(json.dumps({"defaults": {"since_days": 7}, "sources": [
        {"name": "a", "kind": "rss", "url": "u"}, {"name": "b", "kind": "rss", "url": "u", "since_days": 1}]}))
    loaded = feeds.load_sources(path)
    assert loaded[0]["since_days"] == 7 and loaded[1]["since_days"] == 1
