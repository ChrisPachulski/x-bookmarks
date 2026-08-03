"""Export the store to a single SQLite file for the repo.

The JSON store stays the durable, append-only record; the database is a
derived artifact regenerated from it on every export -- compact enough to
commit, so other machines get the content by `git pull` instead of
re-fetching against the API's sliding 800-bookmark window.

Lossless by construction: every full record rides along as a JSON column;
the relational columns exist for querying, not as the source of truth.
"""

import json
import logging
import sqlite3
from pathlib import Path

from .store import iter_records

log = logging.getLogger(__name__)

DEFAULT_DB = Path("bookmarks.db")

SCHEMA = """
CREATE TABLE bookmarks (
    id             TEXT PRIMARY KEY,
    created_at     TEXT,
    author         TEXT,
    text           TEXT,
    lang           TEXT,
    resolve_status TEXT,
    retrieved_at   TEXT,
    record         TEXT NOT NULL
);
CREATE TABLE links (
    bookmark_id TEXT NOT NULL REFERENCES bookmarks(id),
    url         TEXT,
    kind        TEXT,
    status      TEXT,
    reason      TEXT,
    text        TEXT
);
CREATE INDEX links_by_bookmark ON links(bookmark_id);
"""

###############################################################################
# Export
###############################################################################


def export_db(root: Path, db_path: Path = DEFAULT_DB) -> dict[str, int]:
    """Rebuild db_path from the store. Returns row counts.

    Full rewrite each time, in sorted id order, so identical stores produce
    identical bytes and git diffs stay honest.
    """
    tmp = db_path.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    connection = sqlite3.connect(tmp)
    connection.executescript(SCHEMA)

    counts = {"bookmarks": 0, "links": 0}
    for record in sorted(iter_records(root), key=lambda r: r["id"]):
        author = record.get("author") or {}
        connection.execute(
            "INSERT INTO bookmarks VALUES (?,?,?,?,?,?,?,?)",
            (
                record["id"],
                record.get("created_at"),
                author.get("username") if isinstance(author, dict) else str(author),
                record.get("text"),
                record.get("lang"),
                record.get("resolve_status"),
                record.get("retrieved_at"),
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            ),
        )
        counts["bookmarks"] += 1
        for link in record.get("links") or []:
            connection.execute(
                "INSERT INTO links VALUES (?,?,?,?,?,?)",
                (
                    record["id"],
                    link.get("url"),
                    link.get("kind"),
                    link.get("status"),
                    link.get("reason"),
                    link.get("text"),
                ),
            )
            counts["links"] += 1

    connection.commit()
    connection.close()
    tmp.replace(db_path)
    log.info("exported %d bookmarks, %d links -> %s",
             counts["bookmarks"], counts["links"], db_path)
    return counts
