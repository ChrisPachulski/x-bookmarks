"""Append-only disk store for retrieved bookmarks.

The API only ever returns the 800 most recent bookmarks, so this store is the
only durable record beyond that window. Everything here is written to be
non-destructive: a record that has aged out of the API is never removed, and
re-running the pipeline can only ever add or enrich.

Layout:
    store/bookmarks/<id>.json   one self-contained record per bookmark
    store/manifest.jsonl        append-only index, one line per bookmark
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .records import BookmarkRecord, is_bare_link, link_identity, record_status

log = logging.getLogger(__name__)

DEFAULT_STORE = Path("store")


###############################################################################
# Paths
###############################################################################


def bookmarks_dir(root: Path) -> Path:
    return root / "bookmarks"


def manifest_path(root: Path) -> Path:
    return root / "manifest.jsonl"


def record_path(root: Path, bookmark_id: str) -> Path:
    return bookmarks_dir(root) / f"{bookmark_id}.json"


def ensure_store(root: Path) -> Path:
    bookmarks_dir(root).mkdir(parents=True, exist_ok=True)
    return root


###############################################################################
# Atomic write
###############################################################################


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON via a temp file and rename.

    A half-written record is worse than a missing one -- the next run would
    treat it as present and never repair it. Rename is atomic within a
    filesystem, so a reader sees either the old file or the complete new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


###############################################################################
# Reading
###############################################################################


def read_record(root: Path, bookmark_id: str) -> BookmarkRecord | None:
    path = record_path(root, bookmark_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("unreadable record %s: %s", path, exc)
        return None


def known_ids(root: Path) -> set[str]:
    """Every bookmark id already on disk."""
    directory = bookmarks_dir(root)
    if not directory.exists():
        return set()
    return {p.stem for p in directory.glob("*.json")}


def iter_records(root: Path) -> Iterator[BookmarkRecord]:
    """Yield every stored record. The downstream read path."""
    directory = bookmarks_dir(root)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            yield json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("skipping unreadable record %s: %s", path, exc)


###############################################################################
# Writing -- merge, never truncate
###############################################################################


def merge_record(existing: BookmarkRecord, incoming: BookmarkRecord) -> BookmarkRecord:
    """Combine a stored record with a freshly fetched one.

    Incoming wins for volatile fields (metrics change, text does not), but
    resolved link content already on disk is preserved: a re-fetch returns
    links unresolved, and letting that overwrite good content would silently
    destroy the expensive half of the pipeline.
    """
    merged: dict[str, Any] = {**existing, **incoming}

    # Keyed on the t.co short URL, not expanded_url: resolution rewrites
    # expanded_url to the post-redirect destination while the API keeps
    # returning the original, so matching on it silently misses and lets a
    # pending re-fetch overwrite content that was already retrieved.
    existing_links = existing.get("links") or []
    incoming_links = incoming.get("links") or []
    resolved_by_key = {
        link_identity(link): link
        for link in existing_links
        if link.get("status") == "verified" and link.get("text")
    }
    links = []
    for link in incoming_links:
        prior = resolved_by_key.get(link_identity(link))
        # Prior verified content only stands in when the incoming copy would
        # downgrade it; an incoming link that itself carries verified text is
        # a deliberate re-resolution and wins.
        if prior and not (link.get("status") == "verified" and link.get("text")):
            links.append(prior)
        else:
            links.append(link)
    # A link the API omits this time is still part of the durable record --
    # dropping it here would delete stored content the window can no longer
    # return.
    incoming_keys = {link_identity(link) for link in incoming_links}
    links.extend(
        link for link in existing_links
        if link_identity(link) not in incoming_keys
    )
    merged["links"] = links

    # Derived, never inherited. Carrying a status across a merge is how a
    # record ends up marked finished while holding links that were never
    # attempted -- which then makes the resolve stage skip it forever.
    merged["resolve_status"] = record_status(merged.get("links") or [])

    return merged  # type: ignore[return-value]


def put_record(root: Path, record: BookmarkRecord) -> bool:
    """Store one record. Returns True when it is new to the store."""
    bookmark_id = record.get("id") or ""
    if not bookmark_id:
        log.warning("refusing to store a record with no id")
        return False

    existing = read_record(root, bookmark_id)
    payload = merge_record(existing, record) if existing else record
    write_json_atomic(record_path(root, bookmark_id), payload)
    return existing is None



def reset_bare_x_post_links(root: Path) -> int:
    """Knock X-post links whose stored text is only a URL back to pending.

    Earlier builds marked a quoted Article verified while holding nothing but
    its t.co. The merge logic protects verified content, so a normal re-run
    cannot correct them -- they have to be reset explicitly, and only these:
    a link holding real post text is left alone. Any settled status counts,
    since a bare link is never content. Returns records touched.
    """
    repaired = 0
    for record in list(iter_records(root)):
        touched = False
        for link in (record.get("links") or []):
            if (link.get("kind") == "x_post"
                    and link.get("status") != "pending"
                    and is_bare_link(link.get("text") or "")):
                link.update(status="pending", text="", reason="")
                touched = True
        if touched:
            record["resolve_status"] = "pending"
            write_json_atomic(record_path(root, record["id"]), record)
            repaired += 1
    return repaired

def append_manifest(root: Path, record: BookmarkRecord) -> None:
    """Append one index line. Cheap to grep, cheap to stream."""
    author = record.get("author") or {}
    line = {
        "id": record.get("id", ""),
        "created_at": record.get("created_at", ""),
        "author": author.get("username", ""),
        "text": (record.get("text") or "").replace("\n", " ")[:280],
        "links": [
            link.get("expanded_url", "")
            for link in (record.get("links") or [])
        ],
        "retrieved_at": record.get("retrieved_at", ""),
    }
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def save_records(root: Path, records: list[BookmarkRecord]) -> dict[str, int]:
    """Persist a batch. Returns counts of what was new versus updated."""
    ensure_store(root)
    added = 0
    updated = 0
    for record in records:
        if put_record(root, record):
            append_manifest(root, record)
            added += 1
        else:
            updated += 1
    return {"added": added, "updated": updated, "total": len(records)}
