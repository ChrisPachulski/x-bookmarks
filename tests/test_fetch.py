"""Fetch tests -- pagination and the client-side incremental stop.

The bookmarks endpoint has no since_id, only pagination_token, so
"only what is new" has to be decided by the client. These tests pin that
decision, because getting it wrong either re-walks 800 bookmarks every run
or silently misses new ones.
"""

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from x_bookmarks import fetch

FIXTURE = Path(__file__).parent / "fixtures" / "bookmarks_page.json"


def make_page(ids: list[str], next_token: str | None) -> dict:
    meta = {"result_count": len(ids)}
    if next_token:
        meta["next_token"] = next_token
    return {
        "data": [{"id": i, "text": f"post {i}", "author_id": "1"} for i in ids],
        "includes": {"users": [{"id": "1", "username": "u", "name": "U"}]},
        "meta": meta,
    }


def fake_client(pages: list[dict]):
    """A client that hands back the given pages in order."""
    state = {"calls": 0}

    class Client:
        def get_bookmarks(self, **kwargs):
            index = state["calls"]
            state["calls"] += 1
            return pages[index] if index < len(pages) else {}

    client = Client()
    client.calls = state
    return client


###############################################################################
# Pagination
###############################################################################


def test_walks_every_page_when_no_known_ids(monkeypatch) -> None:
    pages = [make_page(["1", "2"], "t1"), make_page(["3", "4"], None)]
    monkeypatch.setattr(fetch, "build_client", lambda token: fake_client(pages))

    result = fetch.fetch_bookmarks("token", known_ids=None)

    assert [r["id"] for r in result["records"]] == ["1", "2", "3", "4"]
    assert result["complete"] is True


def test_stops_at_the_page_cap_and_reports_incomplete(monkeypatch) -> None:
    """A capped walk may have left bookmarks behind; the caller must know,
    because an incremental re-run will early-stop before reaching them."""
    pages = [make_page(["1"], "t1"), make_page(["2"], "t2")]
    monkeypatch.setattr(fetch, "build_client", lambda token: fake_client(pages))

    result = fetch.fetch_bookmarks("token", max_pages=1, known_ids=None)

    assert [r["id"] for r in result["records"]] == ["1"]
    assert result["complete"] is False


def test_empty_first_page_yields_nothing(monkeypatch) -> None:
    monkeypatch.setattr(fetch, "build_client",
                        lambda token: fake_client([{"data": [], "meta": {}}]))
    result = fetch.fetch_bookmarks("token", known_ids=None)
    assert result["records"] == []
    assert result["complete"] is True


def test_a_failing_page_keeps_the_earlier_ones(monkeypatch) -> None:
    """A partial run must still persist what it retrieved -- and say it is
    partial, so the gap can be recovered with --full before it ages out."""
    class Flaky:
        def __init__(self):
            self.calls = 0

        def get_bookmarks(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return make_page(["1", "2"], "t1")
            raise RuntimeError("upstream blew up")

    monkeypatch.setattr(fetch, "build_client", lambda token: Flaky())
    result = fetch.fetch_bookmarks("token", known_ids=None)
    assert [r["id"] for r in result["records"]] == ["1", "2"]
    assert result["complete"] is False


def test_on_page_receives_each_page_as_it_lands(monkeypatch) -> None:
    """Per-page persistence is what makes a long fetch interruption-safe:
    a crash on page N must not lose pages 1..N-1."""
    pages = [make_page(["1", "2"], "t1"), make_page(["3"], None)]
    monkeypatch.setattr(fetch, "build_client", lambda token: fake_client(pages))

    seen: list[list[str]] = []
    fetch.fetch_bookmarks(
        "token", known_ids=None,
        on_page=lambda page_records: seen.append([r["id"] for r in page_records]))

    assert seen == [["1", "2"], ["3"]]


def test_pages_before_a_failure_still_reach_on_page(monkeypatch) -> None:
    class Flaky:
        def __init__(self):
            self.calls = 0

        def get_bookmarks(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return make_page(["1"], "t1")
            raise RuntimeError("upstream blew up")

    monkeypatch.setattr(fetch, "build_client", lambda token: Flaky())
    seen: list[str] = []
    result = fetch.fetch_bookmarks(
        "token", known_ids=None,
        on_page=lambda page_records: seen.extend(r["id"] for r in page_records))
    assert seen == ["1"]
    assert result["complete"] is False


###############################################################################
# Incremental stop
###############################################################################


def test_stops_once_a_page_holds_nothing_new(monkeypatch) -> None:
    """Bookmarks are newest-first, so a fully-known page means done."""
    pages = [make_page(["9", "8"], "t1"), make_page(["7", "6"], "t2"),
             make_page(["5", "4"], None)]
    client = fake_client(pages)
    monkeypatch.setattr(fetch, "build_client", lambda token: client)

    result = fetch.fetch_bookmarks("token", known_ids={"7", "6"})

    assert [r["id"] for r in result["records"]] == ["9", "8", "7", "6"]
    assert result["complete"] is True, "an early stop is sound, not a gap"
    assert client.calls["calls"] == 2, "must not request the third page"


def test_a_partially_known_page_does_not_stop_the_walk(monkeypatch) -> None:
    """One known id among new ones is not a stopping condition."""
    pages = [make_page(["9", "8"], "t1"), make_page(["7", "6"], None)]
    monkeypatch.setattr(fetch, "build_client", lambda token: fake_client(pages))

    result = fetch.fetch_bookmarks("token", known_ids={"8"})

    assert [r["id"] for r in result["records"]] == ["9", "8", "7", "6"]


def test_everything_known_stops_after_one_page(monkeypatch) -> None:
    pages = [make_page(["9", "8"], "t1"), make_page(["7", "6"], None)]
    client = fake_client(pages)
    monkeypatch.setattr(fetch, "build_client", lambda token: client)

    fetch.fetch_bookmarks("token", known_ids={"9", "8", "7", "6"})

    assert client.calls["calls"] == 1


def test_known_ids_none_disables_the_early_stop(monkeypatch) -> None:
    pages = [make_page(["9", "8"], "t1"), make_page(["7", "6"], None)]
    client = fake_client(pages)
    monkeypatch.setattr(fetch, "build_client", lambda token: client)

    fetch.fetch_bookmarks("token", known_ids=None)

    assert client.calls["calls"] == 2


###############################################################################
# Post lookup batching
###############################################################################


def test_post_lookup_batches_by_the_endpoint_limit() -> None:
    seen: list[int] = []

    class Client:
        def get_tweets(self, ids, **kwargs):
            seen.append(len(ids))
            return {"data": [{"id": i, "author_id": "1"} for i in ids],
                    "includes": {"users": [{"id": "1", "username": "u"}]}}

    fetch.fetch_posts(Client(), [str(i) for i in range(250)])

    assert seen == [100, 100, 50]


def test_post_lookup_deduplicates_ids() -> None:
    seen: list[list[str]] = []

    class Client:
        def get_tweets(self, ids, **kwargs):
            seen.append(ids)
            return {"data": []}

    fetch.fetch_posts(Client(), ["1", "1", "2", "2", "1"])

    assert seen == [["1", "2"]]


def test_post_lookup_failure_is_not_fatal() -> None:
    class Client:
        def get_tweets(self, ids, **kwargs):
            raise RuntimeError("rate limited")

    assert fetch.fetch_posts(Client(), ["1"]) == {}


def test_post_lookup_asks_for_long_form_fields() -> None:
    seen: dict = {}

    class Client:
        def get_tweets(self, ids, **kwargs):
            seen.update(kwargs)
            return {"data": []}

    fetch.fetch_posts(Client(), ["1"])

    assert {"note_tweet", "article"} <= set(seen["tweet_fields"])


def test_post_lookup_returns_the_article_body_for_an_article_post() -> None:
    class Client:
        def get_tweets(self, ids, **kwargs):
            return {"data": [{"id": "1", "author_id": "1", "text": "https://t.co/abc",
                              "article": {"title": "T", "plain_text": "Body."}}],
                    "includes": {"users": [{"id": "1", "username": "u"}]}}

    assert fetch.fetch_posts(Client(), ["1"])["1"]["text"] == "T\n\nBody."


def test_post_lookup_returns_the_full_text_of_a_long_post() -> None:
    class Client:
        def get_tweets(self, ids, **kwargs):
            return {"data": [{"id": "1", "author_id": "1", "text": "cut off at",
                              "note_tweet": {"text": "cut off at the end, and more"}}],
                    "includes": {"users": [{"id": "1", "username": "u"}]}}

    assert fetch.fetch_posts(Client(), ["1"])["1"]["text"] == "cut off at the end, and more"


###############################################################################
# Fields that cost nothing on the bookmark
###############################################################################


def captured_bookmark_kwargs(monkeypatch) -> dict:
    seen: dict = {}

    def get_bookmarks(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(fetch, "build_client",
                        lambda token: types.SimpleNamespace(get_bookmarks=get_bookmarks))
    fetch.fetch_bookmarks("token", known_ids=None)
    return seen


def test_bookmark_fetch_asks_for_video_variants(monkeypatch) -> None:
    assert "variants" in captured_bookmark_kwargs(monkeypatch)["media_fields"]


def test_bookmark_fetch_asks_for_the_conversation_id(monkeypatch) -> None:
    assert "conversation_id" in captured_bookmark_kwargs(monkeypatch)["tweet_fields"]


def test_post_lookup_returns_the_conversation_id() -> None:
    def get_tweets(ids, **kwargs):
        return {"data": [{"id": "2", "author_id": "1", "text": "t",
                          "conversation_id": "1"}],
                "includes": {"users": [{"id": "1", "username": "u"}]}}

    client = types.SimpleNamespace(get_tweets=get_tweets)
    assert fetch.fetch_posts(client, ["2"])["2"]["conversation_id"] == "1"


###############################################################################
# Thread walks -- one recent search per conversation
###############################################################################

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
RECENT = "2026-11-10T17:03:36.000Z"
OLD = "2026-10-01T09:00:00.000Z"


def x_record(record_id: str, created_at: str = RECENT, **overrides) -> dict:
    record = {"id": record_id, "text": f"post {record_id}", "created_at": created_at,
              "author": {"id": "1", "username": "writer", "name": "Writer"},
              "referenced": [], "metrics": {"reply_count": 5},
              "conversation_id": record_id, "source": "x_api_v2_bookmarks"}
    record.update(overrides)
    return record


def self_reply(record_id: str, parent: str, conversation_id: str, **overrides) -> dict:
    return x_record(record_id, conversation_id=conversation_id, referenced=[
        {"type": "replied_to", "id": parent, "text": "", "author_username": "writer"}],
        **overrides)


def api_post(post_id: str, parent: str = "") -> dict:
    post = {"id": post_id, "text": f"post {post_id}", "author_id": "1",
            "created_at": RECENT, "conversation_id": "100"}
    if parent:
        post["referenced_tweets"] = [{"type": "replied_to", "id": parent}]
    return post


def thread_client(found: dict | None = None, failing: tuple = (),
                  posts: dict | None = None):
    """A client whose searches return `found[conversation_id]` and whose
    lookups return `posts[id]`. Every call is recorded on `.calls`."""
    calls: dict = {"search": [], "lookup": []}

    def search_recent_tweets(query, **kwargs):
        calls["search"].append({"query": query, **kwargs})
        conversation_id = query.split()[0].split(":", 1)[1]
        if conversation_id in failing:
            raise RuntimeError("429 Too Many Requests")
        return (found or {}).get(conversation_id, {})

    def get_tweets(ids, **kwargs):
        calls["lookup"].append(list(ids))
        data = [{"id": i, "author_id": "1", **(posts or {})[i]}
                for i in ids if i in (posts or {})]
        return {"data": data, "includes": {"users": [{"id": "1", "username": "writer"}]}}

    return types.SimpleNamespace(search_recent_tweets=search_recent_tweets,
                                 get_tweets=get_tweets, calls=calls)


def test_thread_search_asks_for_one_conversation_by_its_author() -> None:
    client = thread_client()
    fetch.fetch_threads(client, [x_record("100")], now=NOW)
    assert client.calls["search"][0]["query"] == "conversation_id:100 from:writer"


def test_thread_search_is_one_page_of_the_endpoint_maximum() -> None:
    client = thread_client()
    fetch.fetch_threads(client, [x_record("100")], now=NOW)
    assert client.calls["search"][0]["max_results"] == 100


def test_thread_search_asks_for_reply_links_and_long_text() -> None:
    client = thread_client()
    fetch.fetch_threads(client, [x_record("100")], now=NOW)
    fields = set(client.calls["search"][0]["tweet_fields"])
    assert {"referenced_tweets", "note_tweet"} <= fields


def test_thread_walk_stores_the_continuation() -> None:
    client = thread_client(found={"100": {"data": [api_post("100"), api_post("101", "100")]}})
    walk = fetch.fetch_threads(client, [x_record("100")], now=NOW)
    assert [p["id"] for p in walk["threads"]["100"]["posts"]] == ["100", "101"]


def test_opening_post_without_replies_costs_no_search() -> None:
    client = thread_client()
    walk = fetch.fetch_threads(client, [x_record("100", metrics={"reply_count": 0})], now=NOW)
    assert client.calls["search"] == []
    assert walk["threads"] == {}


def test_reply_to_another_author_costs_no_search() -> None:
    record = x_record("101", conversation_id="100", referenced=[
        {"type": "replied_to", "id": "100", "text": "", "author_username": "other"}])
    client = thread_client()
    fetch.fetch_threads(client, [record], now=NOW)
    assert client.calls["search"] == []


def test_self_reply_older_than_the_window_is_blocked_without_a_search() -> None:
    client = thread_client()
    walk = fetch.fetch_threads(client, [self_reply("101", "100", "100", created_at=OLD)],
                               now=NOW)
    assert client.calls["search"] == []
    assert walk["threads"]["101"]["status"] == "blocked"
    assert walk["threads"]["101"]["reason"]


def test_opening_post_older_than_the_window_is_left_alone() -> None:
    client = thread_client()
    walk = fetch.fetch_threads(client, [x_record("100", created_at=OLD)], now=NOW)
    assert client.calls["search"] == []
    assert walk["threads"] == {}


def test_failed_search_is_recorded_on_its_thread_and_the_rest_proceed() -> None:
    client = thread_client(failing=("100",))
    walk = fetch.fetch_threads(client, [x_record("100"), x_record("200")], now=NOW)
    assert walk["threads"]["100"]["status"] == "unresolved"
    assert "429" in walk["threads"]["100"]["reason"]
    assert walk["threads"]["200"]["status"] == "verified"


def test_searches_stop_at_the_cap() -> None:
    client = thread_client()
    walk = fetch.fetch_threads(client, [x_record("100"), x_record("200"), x_record("300")],
                               now=NOW, limit=2)
    assert len(client.calls["search"]) == 2
    assert "300" not in walk["threads"]


def test_record_stored_without_a_conversation_id_costs_one_lookup() -> None:
    client = thread_client(posts={"100": {"text": "t", "conversation_id": "100"}})
    walk = fetch.fetch_threads(client, [x_record("100", conversation_id="")], now=NOW)
    assert client.calls["lookup"] == [["100"]]
    assert walk["threads"]["100"]["conversation_id"] == "100"


def test_record_outside_the_window_costs_no_lookup() -> None:
    client = thread_client()
    fetch.fetch_threads(client, [x_record("100", conversation_id="", created_at=OLD)],
                        now=NOW)
    assert client.calls["lookup"] == []


def test_opening_post_is_looked_up_when_the_search_misses_it() -> None:
    client = thread_client(
        found={"100": {"data": [api_post("101", "100"), api_post("102", "101")]}},
        posts={"100": {"text": "the opening post", "conversation_id": "100"}})
    walk = fetch.fetch_threads(client, [self_reply("102", "101", "100")], now=NOW)
    assert client.calls["lookup"] == [["100"]]
    assert [p["id"] for p in walk["threads"]["102"]["posts"]] == ["100", "101", "102"]


def test_feed_records_are_never_walked() -> None:
    client = thread_client()
    walk = fetch.fetch_threads(
        client, [x_record("feed-1", conversation_id="", source="feed:openai-news")], now=NOW)
    assert client.calls == {"search": [], "lookup": []}
    assert walk["threads"] == {}


def test_walk_reports_what_it_cost() -> None:
    client = thread_client(
        found={"100": {"data": [api_post("101", "100"), api_post("102", "101")]}},
        posts={"100": {"text": "the opening post", "conversation_id": "100"}})
    walk = fetch.fetch_threads(client, [self_reply("102", "101", "100")], now=NOW)
    assert (walk["searches"], walk["lookups"]) == (1, 1)
