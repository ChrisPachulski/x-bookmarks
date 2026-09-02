"""Export writes a queryable, lossless SQLite copy of the store."""

import json
import sqlite3

import pytest

from x_bookmarks import store
from x_bookmarks.export import export_db, import_db


def make_record(bookmark_id: str, text: str) -> dict:
    return {
        "id": bookmark_id,
        "text": text,
        "created_at": "2026-08-01T00:00:00.000Z",
        "author": {"username": "someone"},
        "links": [
            {"url": "https://example.com", "kind": "article",
             "status": "verified", "reason": "", "text": "article body"},
        ],
        "resolve_status": "verified",
        "retrieved_at": "2026-08-02T00:00:00Z",
    }


def test_export_roundtrips_records_and_links(tmp_path):
    root = tmp_path / "store"
    store.save_records(root, [make_record("2", "second"), make_record("1", "first")])
    db_path = tmp_path / "bookmarks.db"

    counts = export_db(root, db_path)

    assert counts == {"bookmarks": 2, "links": 2}
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        "SELECT author, text, record FROM bookmarks WHERE id = '1'").fetchone()
    assert row[0] == "someone"
    assert row[1] == "first"
    assert json.loads(row[2])["links"][0]["text"] == "article body"
    link = connection.execute(
        "SELECT status, text FROM links WHERE bookmark_id = '1'").fetchone()
    assert link == ("verified", "article body")


def test_export_is_deterministic(tmp_path):
    root = tmp_path / "store"
    store.save_records(root, [make_record("2", "second"), make_record("1", "first")])
    first, second = tmp_path / "a.db", tmp_path / "b.db"

    export_db(root, first)
    export_db(root, second)

    assert first.read_bytes() == second.read_bytes()


###############################################################################
# Import -- the other machine's half of the round trip
###############################################################################


def test_import_lands_records_a_machine_has_never_seen(tmp_path):
    theirs = tmp_path / "theirs"
    store.save_records(theirs, [make_record("1", "first")])
    db_path = tmp_path / "bookmarks.db"
    export_db(theirs, db_path)

    mine = tmp_path / "mine"
    counts = import_db(mine, db_path)

    assert counts["added"] == 1
    assert store.read_record(mine, "1")["text"] == "first"


def test_import_then_export_is_the_union_of_two_machines(tmp_path):
    theirs = tmp_path / "theirs"
    store.save_records(theirs, [make_record("1", "theirs only")])
    db_path = tmp_path / "bookmarks.db"
    export_db(theirs, db_path)

    mine = tmp_path / "mine"
    store.save_records(mine, [make_record("2", "mine only")])
    import_db(mine, db_path)
    export_db(mine, db_path)

    connection = sqlite3.connect(db_path)
    ids = {row[0] for row in connection.execute("SELECT id FROM bookmarks")}
    assert ids == {"1", "2"}


def test_import_does_not_flatten_locally_resolved_content(tmp_path):
    unresolved = make_record("1", "first")
    unresolved["links"][0].update(status="unresolved", text="")
    theirs = tmp_path / "theirs"
    store.save_records(theirs, [unresolved])
    db_path = tmp_path / "bookmarks.db"
    export_db(theirs, db_path)

    mine = tmp_path / "mine"
    store.save_records(mine, [make_record("1", "first")])
    import_db(mine, db_path)

    assert store.read_record(mine, "1")["links"][0]["text"] == "article body"


def test_import_of_an_older_db_does_not_regress_a_newer_local_record(tmp_path):
    """A pulled db can predate the local store; it must not win on age alone."""
    stale = make_record("1", "first")
    stale["retrieved_at"] = "2026-08-01T00:00:00Z"
    stale["metrics"] = {"like_count": 1}
    stale["links"][0]["text"] = "short older body"
    theirs = tmp_path / "theirs"
    store.save_records(theirs, [stale])
    db_path = tmp_path / "bookmarks.db"
    export_db(theirs, db_path)

    fresh = make_record("1", "first")
    fresh["retrieved_at"] = "2026-08-03T00:00:00Z"
    fresh["metrics"] = {"like_count": 99}
    fresh["links"][0]["text"] = "a much longer, freshly resolved article body"
    mine = tmp_path / "mine"
    store.save_records(mine, [fresh])

    import_db(mine, db_path)

    kept = store.read_record(mine, "1")
    assert kept["retrieved_at"] == "2026-08-03T00:00:00Z"
    assert kept["metrics"] == {"like_count": 99}
    assert kept["links"][0]["text"] == "a much longer, freshly resolved article body"


def test_import_of_a_newer_db_still_advances_the_local_record(tmp_path):
    fresh = make_record("1", "first")
    fresh["retrieved_at"] = "2026-08-03T00:00:00Z"
    fresh["links"][0]["text"] = "newly resolved on the other machine"
    theirs = tmp_path / "theirs"
    store.save_records(theirs, [fresh])
    db_path = tmp_path / "bookmarks.db"
    export_db(theirs, db_path)

    stale = make_record("1", "first")
    stale["retrieved_at"] = "2026-08-01T00:00:00Z"
    mine = tmp_path / "mine"
    store.save_records(mine, [stale])

    import_db(mine, db_path)

    kept = store.read_record(mine, "1")
    assert kept["retrieved_at"] == "2026-08-03T00:00:00Z"
    assert kept["links"][0]["text"] == "newly resolved on the other machine"


def test_import_skips_an_unparseable_row_instead_of_failing(tmp_path):
    root = tmp_path / "store"
    store.save_records(root, [make_record("1", "first")])
    db_path = tmp_path / "bookmarks.db"
    export_db(root, db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO bookmarks VALUES ('2',NULL,NULL,NULL,NULL,NULL,NULL,'{')")
    connection.commit()
    connection.close()

    counts = import_db(tmp_path / "mine", db_path)

    assert counts["skipped"] == 1
    assert counts["added"] == 1


def test_import_raises_when_the_database_is_absent(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_db(tmp_path / "store", tmp_path / "missing.db")
