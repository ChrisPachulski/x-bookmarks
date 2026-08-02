"""Fetch tests -- pagination and the client-side incremental stop.

The bookmarks endpoint has no since_id, only pagination_token, so
"only what is new" has to be decided by the client. These tests pin that
decision, because getting it wrong either re-walks 800 bookmarks every run
or silently misses new ones.
"""

import json
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
