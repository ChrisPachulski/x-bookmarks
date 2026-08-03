"""Export writes a queryable, lossless SQLite copy of the store."""

import json
import sqlite3

from x_bookmarks import store
from x_bookmarks.export import export_db


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
