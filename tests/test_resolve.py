"""Resolution tests. Network is mocked; transform logic is tested directly."""

import pytest

from x_bookmarks import resolve


def make_link(url: str = "https://example.com/a", **overrides) -> dict:
    link = {
        "short_url": "https://t.co/a",
        "expanded_url": url,
        "display_url": "example.com/a",
        "kind": "article",
        "status": "pending",
        "reason": "",
        "title": "",
        "text": "",
    }
    link.update(overrides)
    return link


@pytest.fixture(autouse=True)
def public_hosts(monkeypatch):
    """Keep unit tests off DNS: every host reads as public unless a test
    overrides the guard itself. Yields the real function for tests that
    exercise the guard directly with literal addresses (no DNS needed)."""
    original = resolve.is_private_host
    monkeypatch.setattr(resolve, "is_private_host", lambda host: False)
    yield original


###############################################################################
# YouTube id extraction
###############################################################################


@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
    # v need not be the first query parameter, and hosts arrive mixed-case.
    ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://WWW.YouTube.COM/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://vimeo.com/12345", ""),
    ("", ""),
])
def test_youtube_video_id_extraction(url: str, expected: str) -> None:
    assert resolve.youtube_video_id(url) == expected


###############################################################################
# HTML extraction
###############################################################################


def test_extract_from_html_returns_trafilatura_output(monkeypatch) -> None:
    import sys
    import types

    fake = types.SimpleNamespace(
        extract=lambda html, **kwargs: "Real content",
        extract_metadata=lambda html: types.SimpleNamespace(title="T"),
    )
    monkeypatch.setitem(sys.modules, "trafilatura", fake)
    title, text = resolve.extract_from_html("<p>Real content</p>", "https://example.com")
    assert text == "Real content"
    assert title == "T"


def test_extract_from_html_empty_when_no_body_found(monkeypatch) -> None:
    """No lower-quality fallback: an empty result must stay empty rather than
    a whole-page strip being stored as verified article text."""
    import sys
    import types

    fake = types.SimpleNamespace(
        extract=lambda html, **kwargs: None,
        extract_metadata=lambda html: None,
    )
    monkeypatch.setitem(sys.modules, "trafilatura", fake)
    title, text = resolve.extract_from_html("<nav>chrome only</nav>", "https://example.com")
    assert text == ""


def test_extract_from_html_never_raises_on_garbage() -> None:
    _, text = resolve.extract_from_html("<<<not html>>>", "https://example.com")
    assert isinstance(text, str)


###############################################################################
# Network guards
###############################################################################


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.0.0.8", "192.168.1.1",
                                  "169.254.169.254", ""])
def test_private_and_loopback_hosts_are_flagged(host: str, public_hosts) -> None:
    assert public_hosts(host) is True


def test_public_address_is_not_flagged(public_hosts) -> None:
    assert public_hosts("93.184.216.34") is False  # a literal public IP, no DNS


def test_private_host_link_is_blocked_without_a_fetch(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "is_private_host", lambda host: True)

    def explode(url):
        raise AssertionError("must not fetch a private host")

    monkeypatch.setattr(resolve, "expand_url", explode)
    resolved = resolve.resolve_link(make_link("http://127.0.0.1:8080/admin"))
    assert resolved["status"] == "blocked"
    assert "private" in resolved["reason"]


def test_redirect_to_private_host_is_blocked(monkeypatch) -> None:
    calls = {"n": 0}

    def private_after_redirect(host):
        calls["n"] += 1
        return calls["n"] > 1  # public on first check, private after expand

    monkeypatch.setattr(resolve, "is_private_host", private_after_redirect)
    monkeypatch.setattr(resolve, "expand_url",
                        lambda url: ("http://169.254.169.254/latest/meta-data", ""))

    def explode(url):
        raise AssertionError("must not fetch after a private redirect")

    monkeypatch.setattr(resolve, "extract_article", explode)
    resolved = resolve.resolve_link(make_link("https://bit.ly/short"))
    assert resolved["status"] == "blocked"
    assert "private" in resolved["reason"]


def test_read_capped_truncates_at_the_byte_cap(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "MAX_FETCH_BYTES", 100)

    class Streamed:
        def iter_content(self, chunk_size):
            for _ in range(5):
                yield b"x" * 64

    body, truncated = resolve.read_capped(Streamed())
    assert truncated is True
    assert len(body) <= 128


def test_read_capped_reads_small_bodies_whole() -> None:
    class Streamed:
        def iter_content(self, chunk_size):
            yield b"hello"

    body, truncated = resolve.read_capped(Streamed())
    assert body == b"hello"
    assert truncated is False


###############################################################################
# Record-level status rollup
###############################################################################


def test_no_links_is_a_complete_record() -> None:
    assert resolve.record_status([]) == "verified"


def test_all_links_verified_is_verified() -> None:
    links = [make_link(status="verified"), make_link(status="verified")]
    assert resolve.record_status(links) == "verified"


def test_partial_success_is_inferred_not_verified() -> None:
    """Honest status: some content missing must not read as fully retrieved."""
    links = [make_link(status="verified"), make_link(status="unresolved")]
    assert resolve.record_status(links) == "inferred"


def test_inferred_only_is_inferred_not_unresolved() -> None:
    links = [make_link(status="inferred", text="https://t.co/abc")]
    assert resolve.record_status(links) == "inferred"


def test_all_blocked_is_blocked() -> None:
    links = [make_link(status="blocked"), make_link(status="blocked")]
    assert resolve.record_status(links) == "blocked"


def test_all_failed_is_unresolved() -> None:
    links = [make_link(status="unresolved")]
    assert resolve.record_status(links) == "unresolved"


###############################################################################
# Per-link resolution
###############################################################################


def test_link_without_url_is_blocked() -> None:
    resolved = resolve.resolve_link(make_link("", short_url=""))
    assert resolved["status"] == "blocked"
    assert "no URL" in resolved["reason"]


def test_image_link_is_blocked_with_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url",
                        lambda url: ("https://cdn.example.com/a.jpg", ""))
    resolved = resolve.resolve_link(make_link("https://cdn.example.com/a.jpg"))
    assert resolved["status"] == "blocked"
    assert "image" in resolved["reason"]


def test_article_success_stores_text_and_clears_reason(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    monkeypatch.setattr(resolve, "extract_article",
                        lambda url: ("Title", "The body", ""))
    resolved = resolve.resolve_link(make_link())
    assert resolved["status"] == "verified"
    assert resolved["text"] == "The body"
    assert resolved["title"] == "Title"
    assert resolved["reason"] == ""


def test_article_failure_records_the_reason(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    monkeypatch.setattr(resolve, "extract_article",
                        lambda url: ("", "", "paywalled or legally blocked (HTTP 402)"))
    resolved = resolve.resolve_link(make_link())
    assert resolved["status"] == "unresolved"
    assert "paywalled" in resolved["reason"]


def test_redirect_updates_the_stored_url_and_kind(monkeypatch) -> None:
    """A tracking wrapper must not be stored as the canonical location."""
    monkeypatch.setattr(
        resolve, "expand_url",
        lambda url: ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ""))
    monkeypatch.setattr(resolve, "extract_transcript",
                        lambda url: ("transcript text", ""))
    resolved = resolve.resolve_link(make_link("https://tracker.example/r?id=1"))
    assert resolved["expanded_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert resolved["kind"] == "video"
    assert resolved["text"] == "transcript text"


def test_video_without_transcript_is_unresolved(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    monkeypatch.setattr(resolve, "extract_transcript",
                        lambda url: ("", "TranscriptsDisabled"))
    resolved = resolve.resolve_link(
        make_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
    assert resolved["status"] == "unresolved"
    assert "TranscriptsDisabled" in resolved["reason"]


###############################################################################
# X posts -- API only, never scraped
###############################################################################

X_URL = "https://twitter.com/someone/status/1750000000000000001"


def test_x_post_never_hits_the_network(monkeypatch) -> None:
    """Scraping x.com returns reply metadata, not the post. It must not run."""
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))

    def explode(url):
        raise AssertionError("x.com must never be fetched over HTTP")

    monkeypatch.setattr(resolve, "extract_article", explode)
    resolved = resolve.resolve_link(make_link(X_URL, kind="x_post"))
    assert resolved["status"] == "unresolved"


def test_x_post_resolves_from_the_lookup(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    lookup = {"1750000000000000001": {"text": "the real post text",
                                      "author_username": "someone"}}
    resolved = resolve.resolve_link(make_link(X_URL), post_lookup=lookup)
    assert resolved["status"] == "verified"
    assert resolved["text"] == "the real post text"
    assert resolved["title"] == "@someone"


def test_x_post_absent_from_lookup_is_unresolved_with_a_reason(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    resolved = resolve.resolve_link(make_link(X_URL), post_lookup={})
    assert resolved["status"] == "unresolved"
    assert "1750000000000000001" in resolved["reason"]
    assert resolved["text"] == ""


def test_x_post_whose_text_is_only_a_link_is_inferred_not_verified(monkeypatch) -> None:
    """An Article's post text is just its t.co. Holding the URL is partial
    success -- calling it verified reports content that was never stored."""
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    lookup = {"1750000000000000001": {"text": "https://t.co/LSAMpgaool",
                                      "author_username": "someone"}}
    resolved = resolve.resolve_link(make_link(X_URL), post_lookup=lookup)
    assert resolved["status"] == "inferred"
    assert resolved["reason"]


def test_x_link_without_a_post_id_is_unresolved(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    resolved = resolve.resolve_link(make_link("https://x.com/someuser"))
    assert resolved["status"] == "unresolved"
    assert "no post id" in resolved["reason"]


ARTICLE_URL = "https://x.com/i/article/2056513669886771200"


def test_article_link_resolves_from_the_body_on_its_own_record() -> None:
    """The whole point of the project: long-form Articles must be readable.

    The body rides in on the bookmark via the `article` tweet field, so the
    link is satisfied from the record rather than a lookup that cannot work.
    """
    record = {
        "id": "1",
        "links": [make_link(ARTICLE_URL, kind="x_post")],
        "article": {"title": "The Piece", "text": "the full prose body",
                    "preview": "the full..."},
    }
    resolved = resolve.resolve_record(record)
    link = resolved["links"][0]
    assert link["status"] == "verified"
    assert link["text"] == "the full prose body"
    assert link["title"] == "The Piece"
    assert resolved["resolve_status"] == "verified"


def test_article_link_without_a_body_on_the_record_is_blocked(monkeypatch) -> None:
    """Someone else's Article: no read endpoint is keyed by article id."""
    monkeypatch.setattr(resolve, "expand_url", lambda url: (url, ""))
    resolved = resolve.resolve_link(make_link(ARTICLE_URL), post_lookup={})
    assert resolved["status"] == "blocked"
    assert "another post" in resolved["reason"]


def test_x_article_never_costs_an_api_lookup() -> None:
    """A guaranteed Not Found should not be paid for on every run."""
    records = [{"links": [make_link(ARTICLE_URL, kind="x_post")]}]
    assert resolve.missing_x_post_ids(records, {}) == []


###############################################################################
# Lookup seeding
###############################################################################


def test_lookup_is_seeded_from_quoted_post_text() -> None:
    records = [{"referenced": [{"id": "111", "text": "quoted body",
                                "author_username": "quoted_author"}]}]
    lookup = resolve.post_lookup_from_records(records)
    assert lookup["111"]["text"] == "quoted body"
    assert lookup["111"]["author_username"] == "quoted_author"


def test_lookup_is_not_seeded_from_a_bare_link_reference() -> None:
    """A quoted Article's post text is only its t.co. Seeding from it would
    stop the lookup that can actually return the body."""
    record = {"id": "1", "links": [], "referenced": [
        {"type": "quoted", "id": "8", "text": "https://t.co/abc", "author_username": "a"}]}
    assert resolve.post_lookup_from_records([record]) == {}


def test_reference_without_text_is_not_seeded() -> None:
    records = [{"referenced": [{"id": "111", "text": ""}]}]
    assert resolve.post_lookup_from_records(records) == {}


def test_missing_ids_excludes_what_is_already_seeded() -> None:
    records = [{"links": [make_link(X_URL, kind="x_post")]}]
    seeded = {"1750000000000000001": {"text": "have it"}}
    assert resolve.missing_x_post_ids(records, seeded) == []


def test_missing_ids_reports_what_needs_a_lookup() -> None:
    records = [{"links": [make_link(X_URL, kind="x_post")]}]
    assert resolve.missing_x_post_ids(records, {}) == ["1750000000000000001"]


def test_missing_ids_deduplicates_across_records() -> None:
    records = [
        {"links": [make_link(X_URL, kind="x_post")]},
        {"links": [make_link(X_URL, kind="x_post")]},
    ]
    assert resolve.missing_x_post_ids(records, {}) == ["1750000000000000001"]


def test_missing_ids_skips_already_verified_links() -> None:
    records = [{"links": [make_link(X_URL, kind="x_post",
                                    status="verified", text="body")]}]
    assert resolve.missing_x_post_ids(records, {}) == []


###############################################################################
# PDF extraction
###############################################################################


def test_pdf_without_pypdf_reports_the_missing_dependency(monkeypatch) -> None:
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("no pypdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    text, reason = resolve.extract_pdf(b"%PDF-1.4")
    assert text == ""
    assert "pypdf" in reason


def test_unparseable_pdf_reports_a_reason() -> None:
    text, reason = resolve.extract_pdf(b"not a pdf at all")
    assert text == ""
    assert "PDF parse failed" in reason


###############################################################################
# Record-level resolution
###############################################################################


def test_already_verified_links_are_not_refetched(monkeypatch) -> None:
    def explode(link):
        raise AssertionError("should not re-resolve verified content")

    monkeypatch.setattr(resolve, "resolve_link", explode)
    record = {"id": "1", "links": [make_link(status="verified", text="body")]}
    resolved = resolve.resolve_record(record)
    assert resolved["links"][0]["text"] == "body"


def test_all_flag_forces_reresolution(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "resolve_link",
                        lambda link, post_lookup=None: {
                            **link, "text": "refreshed", "status": "verified"})
    record = {"id": "1", "links": [make_link(status="verified", text="old")]}
    resolved = resolve.resolve_record(record, skip_resolved=False)
    assert resolved["links"][0]["text"] == "refreshed"


def test_record_with_no_links_is_verified() -> None:
    assert resolve.resolve_record({"id": "1", "links": []})["resolve_status"] == "verified"


def test_one_failing_record_does_not_abort_the_batch(monkeypatch) -> None:
    def fail_on_second(record, skip_resolved=True):
        if record["id"] == "2":
            raise RuntimeError("boom")
        return {**record, "resolve_status": "verified"}

    monkeypatch.setattr(resolve, "resolve_record", fail_on_second)
    out = resolve.resolve_records(
        [{"id": "1", "links": []}, {"id": "2", "links": []}, {"id": "3", "links": []}])
    assert [r["id"] for r in out] == ["1", "2", "3"]
    assert out[1]["resolve_status"] == "unresolved"


def test_persist_callback_runs_per_record(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "resolve_record",
                        lambda record, skip_resolved=True: record)
    seen: list[str] = []
    resolve.resolve_records(
        [{"id": "1", "links": []}, {"id": "2", "links": []}],
        on_resolved=lambda record: seen.append(record["id"]))
    assert seen == ["1", "2"]


def test_failing_callback_does_not_abort_the_batch(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "resolve_record",
                        lambda record, skip_resolved=True: record)

    def bad_callback(record):
        raise OSError("disk full")

    out = resolve.resolve_records(
        [{"id": "1", "links": []}, {"id": "2", "links": []}],
        on_resolved=bad_callback)
    assert len(out) == 2
