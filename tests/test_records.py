"""Normalization tests -- one API page into self-contained records."""

import json
from pathlib import Path

import pytest

from x_bookmarks import records

FIXTURE = Path(__file__).parent / "fixtures" / "bookmarks_page.json"
RETRIEVED_AT = "2026-07-29T12:00:00Z"


@pytest.fixture
def page() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def built(page: dict) -> list:
    return records.build_page(page["data"], page["includes"], RETRIEVED_AT)


###############################################################################
# Link classification
###############################################################################


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/posts/thing", "article"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video"),
    ("https://youtu.be/dQw4w9WgXcQ", "video"),
    ("https://vimeo.com/12345", "video"),
    ("https://twitter.com/a/status/1", "x_post"),
    ("https://x.com/a/status/1", "x_post"),
    ("https://cdn.example.com/photo.jpg", "image"),
    ("https://cdn.example.com/photo.PNG", "image"),
    ("not a url", "unknown"),
    ("", "unknown"),
])
def test_classify_link_routes_by_host_and_suffix(url: str, expected: str) -> None:
    assert records.classify_link(url) == expected


def test_classify_link_ignores_query_string_when_checking_suffix() -> None:
    assert records.classify_link("https://cdn.example.com/p.jpg?v=2") == "image"


def test_url_host_strips_www_and_lowercases() -> None:
    assert records.url_host("HTTPS://WWW.Example.COM/path") == "example.com"


def test_url_host_strips_ports_and_credentials() -> None:
    """youtube.com:443 must classify as youtube.com, not fall through."""
    assert records.url_host("https://www.youtube.com:443/watch?v=x") == "youtube.com"
    assert records.url_host("https://user:pass@example.com/x") == "example.com"


def test_url_host_returns_empty_for_unparseable() -> None:
    assert records.url_host("nonsense") == ""


###############################################################################
# Record building
###############################################################################


def test_build_page_returns_one_record_per_tweet(built: list) -> None:
    assert len(built) == 4


def test_author_is_joined_from_includes(built: list) -> None:
    assert built[0]["author"] == {
        "id": "44196397", "username": "writer_one", "name": "Writer One",
    }


def test_article_link_is_extracted_and_classified(built: list) -> None:
    links = built[0]["links"]
    assert len(links) == 1
    assert links[0]["expanded_url"] == "https://example.com/posts/agent-tool-discovery"
    assert links[0]["kind"] == "article"
    assert links[0]["status"] == "pending"


def test_video_link_is_classified_as_video(built: list) -> None:
    assert built[1]["links"][0]["kind"] == "video"


def test_self_referential_media_link_is_dropped(built: list) -> None:
    """A tweet's own photo permalink is a placeholder, not outbound content."""
    assert built[2]["links"] == []


def test_media_is_joined_from_includes(built: list) -> None:
    media = built[2]["media"]
    assert len(media) == 1
    assert media[0]["kind"] == "photo"
    assert media[0]["url"] == "https://pbs.twimg.com/media/example.jpg"


def test_referenced_tweets_are_recorded(built: list) -> None:
    reference = built[3]["referenced"][0]
    assert reference["type"] == "quoted"
    assert reference["id"] == "1749000000000000099"


def test_quoted_post_text_is_captured_from_includes(built: list) -> None:
    """The text arrives free in the same response -- discarding it would
    force a second lookup for something already paid for."""
    reference = built[3]["referenced"][0]
    assert reference["text"] == "The original post being quoted"
    assert reference["author_username"] == "talker_two"


def test_quoted_post_missing_from_includes_keeps_the_id() -> None:
    tweet = {"id": "9", "referenced_tweets": [{"type": "quoted", "id": "404"}]}
    built = records.build_page([tweet], None, RETRIEVED_AT)
    reference = built[0]["referenced"][0]
    assert reference["id"] == "404"
    assert reference["text"] == ""


###############################################################################
# X permalink parsing
###############################################################################


@pytest.mark.parametrize("url,expected", [
    ("https://twitter.com/user/status/1750000000000000001", "1750000000000000001"),
    ("https://x.com/user/status/1750000000000000001", "1750000000000000001"),
    ("https://x.com/user/statuses/1750000000000000001", "1750000000000000001"),
    ("https://x.com/user/status/1750000000000000001?s=20", "1750000000000000001"),
    ("https://x.com/i/article/2056513669886771200", "2056513669886771200"),
    ("https://x.com/i/web/status/1750000000000000001", "1750000000000000001"),
    ("https://x.com/someuser", ""),
    ("https://example.com/status/123", ""),
    ("", ""),
])
def test_status_id_from_x_url(url: str, expected: str) -> None:
    assert records.status_id_from_x_url(url) == expected


def test_tweet_without_links_gets_empty_link_list(built: list) -> None:
    assert built[3]["links"] == []


def test_every_record_carries_retrieval_provenance(built: list) -> None:
    for record in built:
        assert record["retrieved_at"] == RETRIEVED_AT
        assert record["source"] == "x_api_v2_bookmarks"


def test_metrics_are_preserved(built: list) -> None:
    assert built[0]["metrics"]["bookmark_count"] == 22


###############################################################################
# Resilience
###############################################################################


def test_malformed_tweet_is_skipped_not_fatal() -> None:
    page = [{"id": "1", "text": "fine"}, {"entities": None, "id": "2"}]
    built = records.build_page(page, None, RETRIEVED_AT)
    assert [record["id"] for record in built] == ["1", "2"]


def test_tweet_without_id_is_dropped() -> None:
    built = records.build_page([{"text": "no id"}], None, RETRIEVED_AT)
    assert built == []


def test_duplicate_urls_are_deduplicated() -> None:
    tweet = {
        "id": "9",
        "entities": {"urls": [
            {"url": "https://t.co/a", "expanded_url": "https://example.com/x"},
            {"url": "https://t.co/b", "expanded_url": "https://example.com/x"},
        ]},
    }
    built = records.build_page([tweet], None, RETRIEVED_AT)
    assert len(built[0]["links"]) == 1


def test_index_includes_tolerates_missing_includes() -> None:
    lookups = records.index_includes(None)
    assert lookups == {"users": {}, "media": {}, "tweets": {}}


###############################################################################
# Long-form text -- note_tweet and quoted Articles
###############################################################################

LONG_POST = {
    "id": "7",
    "text": "first 280 characters, cut off at the",
    "note_tweet": {"text": "first 280 characters, cut off at the end, then the rest"},
}
ARTICLE_POST = {
    "id": "8",
    "text": "https://t.co/abc123",
    "article": {"title": "Harness Engineering", "plain_text": "The whole piece.",
                "preview_text": "The whole"},
}


def test_long_post_text_comes_from_note_tweet() -> None:
    built = records.build_page([LONG_POST], None, RETRIEVED_AT)
    assert built[0]["text"] == "first 280 characters, cut off at the end, then the rest"


def test_quoted_long_post_text_comes_from_note_tweet() -> None:
    tweet = {"id": "9", "referenced_tweets": [{"type": "quoted", "id": "7"}]}
    built = records.build_page([tweet], {"tweets": [LONG_POST]}, RETRIEVED_AT)
    assert built[0]["referenced"][0]["text"] == (
        "first 280 characters, cut off at the end, then the rest")


def test_quoted_article_body_is_carried_into_referenced_text() -> None:
    """A quoted Article's post text is a bare t.co; the body is on the
    included tweet in the same response and must not be thrown away."""
    tweet = {"id": "9", "referenced_tweets": [{"type": "quoted", "id": "8"}]}
    built = records.build_page([tweet], {"tweets": [ARTICLE_POST]}, RETRIEVED_AT)
    text = built[0]["referenced"][0]["text"]
    assert text.startswith("Harness Engineering")
    assert "The whole piece." in text
    assert "https://t.co/abc123" not in text


@pytest.mark.parametrize("text,expected", [
    ("https://t.co/LSAMpgaool", True),
    ("  https://t.co/LSAMpgaool\n", True),
    ("https://x.com/i/article/2088576943155290112", True),
    ("Read this https://t.co/LSAMpgaool", False),
    ("", False),
])
def test_is_bare_link(text: str, expected: bool) -> None:
    assert records.is_bare_link(text) is expected
