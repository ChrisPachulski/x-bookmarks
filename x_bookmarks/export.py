"""Move the store in and out of a single SQLite file for the repo.

The JSON store stays the durable, append-only record; the database is a
derived artifact regenerated from it on every export -- compact enough to
commit, so other machines get the content by `git pull` instead of
re-fetching against the API's sliding 800-bookmark window.

Lossless by construction: every full record rides along as a JSON column;
the relational columns exist for querying, not as the source of truth.

Because it is lossless, the traffic runs both ways. `import_db` reads a
pulled database back into the local store through `save_records`, so two
machines fetching independently converge instead of overwriting each other:
whichever database wins a binary merge conflict, importing it and exporting
again yields the union. Without that leg an export on a machine that has not
seen the other's bookmarks silently reverts them out of the committed file.
"""

import json
import logging
import sqlite3
from pathlib import Path

from .store import (
    BookmarkRecord,
    iter_records,
    merge_record,
    read_record,
    save_records,
)

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


###############################################################################
# Import
###############################################################################


def read_db_records(db_path: Path) -> tuple[list[BookmarkRecord], int]:
    """Pull full records out of a database. Returns records and a skip count.

    Only the JSON `record` column is read -- the relational columns are a
    query convenience and a lossy view of it. A row whose JSON will not parse
    is skipped rather than fatal: one corrupt row must not cost the import.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, record FROM bookmarks ORDER BY id").fetchall()
    finally:
        connection.close()

    records: list[BookmarkRecord] = []
    skipped = 0
    for bookmark_id, payload in rows:
        try:
            record = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            log.warning("skipping %s: record column is not valid JSON",
                        bookmark_id)
            skipped += 1
            continue
        if not isinstance(record, dict) or not record.get("id"):
            log.warning("skipping %s: record column holds no usable record",
                        bookmark_id)
            skipped += 1
            continue
        records.append(record)  # type: ignore[arg-type]

    return records, skipped


def retrieval_time(record: BookmarkRecord) -> str:
    """Sortable retrieval timestamp. Missing sorts earliest, as ISO-8601 does."""
    return record.get("retrieved_at") or ""


def order_chronologically(
    existing: BookmarkRecord, incoming: BookmarkRecord,
) -> BookmarkRecord:
    """Merge two copies of one record oldest-first.

    `merge_record` gives the incoming copy the scalar fields and lets an
    incoming verified link win as a deliberate re-resolution. That is right
    for a fetch, where incoming is by definition newer, and wrong for an
    import: a pulled database can predate the local store, and feeding it in
    as `incoming` regresses metrics and retrieved_at and overwrites resolved
    link text with a staler, shorter copy. Ordering by retrieved_at keeps the
    existing contract and points it the right way round.
    """
    if retrieval_time(incoming) < retrieval_time(existing):
        return merge_record(incoming, existing)
    return merge_record(existing, incoming)


def import_db(root: Path, db_path: Path = DEFAULT_DB) -> dict[str, int]:
    """Merge a database into the store. Returns counts.

    Still routed through `save_records`, so the append-only guarantee and the
    no-downgrade rule hold; the pre-merge above only settles which of two
    copies is the newer before that runs.
    """
    records, skipped = read_db_records(db_path)
    reconciled: list[BookmarkRecord] = []
    for record in records:
        existing = read_record(root, record["id"])
        reconciled.append(
            order_chronologically(existing, record) if existing else record)

    counts = save_records(root, reconciled)
    counts["skipped"] = skipped
    log.info("imported %d bookmarks from %s (%d new, %d merged, %d skipped)",
             counts["total"], db_path, counts["added"], counts["updated"],
             skipped)
    return counts
