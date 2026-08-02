"""Store tests -- the append-only guarantee is the property that matters.

The API only returns the 800 most recent bookmarks, so the store is the only
durable copy. A bug that lets a re-run destroy stored content is not a
cosmetic bug; it is permanent data loss.
"""

import json
from pathlib import Path

import pytest

from x_bookmarks import store


def make_record(bookmark_id: str = "1", **overrides) -> dict:
    record = {
        "id": bookmark_id,
        "text": "a bookmark",
        "created_at": "2026-07-20T14:03:11.000Z",
        "lang": "en",
        "author": {"id": "9", "username": "writer", "name": "Writer"},
        "links": [],
        "media": [],
        "metrics": {"like_count": 1},
        "referenced": [],
        "resolve_status": "pending",
        "retrieved_at": "2026-07-29T12:00:00Z",
        "source": "x_api_v2_bookmarks",
    }
    record.update(overrides)
    return record


def make_link(url: str = "https://example.com/a", **overrides) -> dict:
    # short_url must vary with the target: it is the merge key, so a shared
    # constant would collide every link in a record onto one identity.
    link = {
        "short_url": f"https://t.co/{abs(hash(url)) % 10**8}",
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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return store.ensure_store(tmp_path / "store")


###############################################################################
# Round trip
###############################################################################


def test_put_record_then_read_record_round_trips(root: Path) -> None:
    store.put_record(root, make_record("42"))
    assert store.read_record(root, "42")["text"] == "a bookmark"


def test_put_record_reports_true_only_the_first_time(root: Path) -> None:
    assert store.put_record(root, make_record("1")) is True
    assert store.put_record(root, make_record("1")) is False


def test_record_with_no_id_is_refused(root: Path) -> None:
    assert store.put_record(root, make_record("")) is False
    assert list(store.iter_records(root)) == []


def test_read_record_returns_none_when_absent(root: Path) -> None:
    assert store.read_record(root, "nope") is None


def test_known_ids_lists_stored_records(root: Path) -> None:
    store.put_record(root, make_record("1"))
    store.put_record(root, make_record("2"))
    assert store.known_ids(root) == {"1", "2"}


def test_known_ids_is_empty_for_missing_store(tmp_path: Path) -> None:
    assert store.known_ids(tmp_path / "absent") == set()


###############################################################################
# The append-only guarantee
###############################################################################


def test_refetch_does_not_erase_resolved_content(root: Path) -> None:
    """The core protection: a re-fetch returns links unresolved.

    Letting that overwrite stored text would silently destroy the expensive
    half of the pipeline on every run.
    """
    resolved = make_link(status="verified", text="the full article body")
    store.put_record(root, make_record("1", links=[resolved],
                                       resolve_status="verified"))

    refetched = make_link(status="pending", text="")
    store.put_record(root, make_record("1", links=[refetched],
                                       resolve_status="pending"))

    stored = store.read_record(root, "1")
    assert stored["links"][0]["text"] == "the full article body"
    assert stored["links"][0]["status"] == "verified"
    assert stored["resolve_status"] == "verified"


def test_refetch_updates_volatile_metrics(root: Path) -> None:
    store.put_record(root, make_record("1", metrics={"like_count": 1}))
    store.put_record(root, make_record("1", metrics={"like_count": 99}))
    assert store.read_record(root, "1")["metrics"]["like_count"] == 99


def test_newly_added_link_survives_alongside_resolved_one(root: Path) -> None:
    old = make_link("https://example.com/a", status="verified", text="body A")
    store.put_record(root, make_record("1", links=[old]))

    new = make_link("https://example.com/b")
    store.put_record(root, make_record(
        "1", links=[make_link("https://example.com/a"), new]))

    links = {link["expanded_url"]: link for link in store.read_record(root, "1")["links"]}
    assert links["https://example.com/a"]["text"] == "body A"
    assert links["https://example.com/b"]["status"] == "pending"


def test_refetch_does_not_downgrade_a_settled_status(root: Path) -> None:
    """A re-fetch always arrives pending; it must not un-finish a record."""
    store.put_record(root, make_record("1", links=[], resolve_status="verified"))
    store.put_record(root, make_record("1", links=[], resolve_status="pending"))
    assert store.read_record(root, "1")["resolve_status"] == "verified"


def test_status_is_derived_from_links_not_inherited(root: Path) -> None:
    """A re-fetch that brings unattempted links makes the record pending.

    Inheriting the old status here is what let records read `verified` while
    holding links nothing had ever tried, which then made the resolve stage
    skip them permanently.
    """
    blocked = make_link(status="blocked", reason="image, no text to extract")
    store.put_record(root, make_record("1", links=[blocked],
                                       resolve_status="blocked"))
    store.put_record(root, make_record("1", links=[make_link()],
                                       resolve_status="pending"))
    assert store.read_record(root, "1")["resolve_status"] == "pending"


def test_resolved_content_survives_a_url_rewriting_redirect(root: Path) -> None:
    """The bug this keys on: resolution rewrites expanded_url to the
    post-redirect destination, while a re-fetch keeps returning the original.
    Matching on expanded_url therefore misses, and pending overwrites good
    content. The t.co short URL is stable, so it is the merge key."""
    stored = make_link("https://tracker.example/r?id=1",
                       status="verified", text="the article body")
    stored["expanded_url"] = "https://real-destination.example/post"
    store.put_record(root, make_record("1", links=[stored]))

    refetched = make_link("https://tracker.example/r?id=1")
    store.put_record(root, make_record("1", links=[refetched]))

    link = store.read_record(root, "1")["links"][0]
    assert link["text"] == "the article body"
    assert link["status"] == "verified"


def test_resolve_stage_can_still_advance_a_status(root: Path) -> None:
    """Protection is one-directional -- real progress must still land."""
    store.put_record(root, make_record("1", links=[], resolve_status="pending"))
    store.put_record(root, make_record("1", links=[], resolve_status="verified"))
    assert store.read_record(root, "1")["resolve_status"] == "verified"


def test_unresolved_link_is_allowed_to_be_replaced(root: Path) -> None:
    """Only verified content is protected -- a failed attempt should retry."""
    failed = make_link(status="unresolved", reason="HTTP 500")
    store.put_record(root, make_record("1", links=[failed]))

    succeeded = make_link(status="verified", text="body now available")
    store.put_record(root, make_record("1", links=[succeeded]))

    assert store.read_record(root, "1")["links"][0]["text"] == "body now available"


###############################################################################
# Manifest
###############################################################################


def test_manifest_gets_one_line_per_new_bookmark(root: Path) -> None:
    store.save_records(root, [make_record("1"), make_record("2")])
    lines = store.manifest_path(root).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == "1"


def test_manifest_does_not_duplicate_on_refetch(root: Path) -> None:
    store.save_records(root, [make_record("1")])
    store.save_records(root, [make_record("1")])
    lines = store.manifest_path(root).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_manifest_truncates_long_text_and_flattens_newlines(root: Path) -> None:
    store.save_records(root, [make_record("1", text="a\nb" + "x" * 500)])
    line = json.loads(store.manifest_path(root).read_text(encoding="utf-8"))
    assert "\n" not in line["text"]
    assert len(line["text"]) <= 280


def test_save_records_reports_added_and_updated(root: Path) -> None:
    store.save_records(root, [make_record("1")])
    counts = store.save_records(root, [make_record("1"), make_record("2")])
    assert counts == {"added": 1, "updated": 1, "total": 2}


###############################################################################
# Durability
###############################################################################


def test_unreadable_record_is_skipped_not_fatal(root: Path) -> None:
    store.put_record(root, make_record("1"))
    (store.bookmarks_dir(root) / "broken.json").write_text("{ not json",
                                                          encoding="utf-8")
    assert [record["id"] for record in store.iter_records(root)] == ["1"]


def test_write_json_atomic_leaves_no_temp_files(root: Path) -> None:
    store.write_json_atomic(root / "x.json", {"a": 1})
    assert list(root.glob("*.tmp")) == []


def test_write_json_atomic_preserves_old_file_on_failure(root: Path) -> None:
    path = root / "x.json"
    store.write_json_atomic(path, {"good": True})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        store.write_json_atomic(path, {"bad": Unserializable()})

    assert json.loads(path.read_text(encoding="utf-8")) == {"good": True}
    assert list(root.glob("*.tmp")) == []


###############################################################################
# Merge completeness -- the durable record survives a shifting API payload
###############################################################################


def test_link_omitted_by_a_refetch_is_not_deleted(root: Path) -> None:
    """The API sometimes drops an entity from a record it returns again.
    The stored link -- and its resolved text -- must survive that."""
    kept = make_link("https://example.com/a", status="verified", text="body A")
    other = make_link("https://example.com/b", status="verified", text="body B")
    store.put_record(root, make_record("1", links=[kept, other]))

    refetched_without_b = make_record(
        "1", links=[make_link("https://example.com/a")])
    store.put_record(root, refetched_without_b)

    links = {link["expanded_url"]: link
             for link in store.read_record(root, "1")["links"]}
    assert links["https://example.com/b"]["text"] == "body B"
    assert links["https://example.com/a"]["text"] == "body A"


def test_a_deliberate_reresolution_replaces_verified_content(root: Path) -> None:
    """resolve --all must be able to land on disk: incoming verified text is
    a re-resolution, not a downgrade, and wins over the stored copy."""
    old = make_link("https://example.com/a", status="verified", text="stale body")
    store.put_record(root, make_record("1", links=[old]))

    refreshed = make_link("https://example.com/a", status="verified",
                          text="refreshed body")
    store.put_record(root, make_record("1", links=[refreshed]))

    assert store.read_record(root, "1")["links"][0]["text"] == "refreshed body"
