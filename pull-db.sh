#!/bin/sh
###############################################################################
# Pull another machine's bookmarks.db over SSH and bring it in here, no git.
#
#   ./pull-db.sh user@host:x-bookmarks/bookmarks.db
#   X_BOOKMARKS_PEER=user@host:x-bookmarks/bookmarks.db ./pull-db.sh
#
# The peer's file lands in .incoming/peer.db (kept, so the next pull is an
# rsync delta). It never overwrites bookmarks.db blindly:
#   - with a store and .venv here, it is merged through `cli.py import`
#     (append-only, no status downgrade) and bookmarks.db is re-exported;
#   - without a store, bookmarks.db is replaced only if the pulled copy holds
#     every bookmark id the local copy has. Otherwise it stops.
###############################################################################

set -eu

SOURCE="${1:-${X_BOOKMARKS_PEER:-}}"
if [ -z "$SOURCE" ]; then
    echo "FAILED: no source; pass user@host:x-bookmarks/bookmarks.db or set X_BOOKMARKS_PEER" >&2
    exit 2
fi

REPO="$(cd "$(dirname "$0")" && pwd)"
INCOMING="$REPO/.incoming"
PULLED="$INCOMING/peer.db"
LOCAL="$REPO/bookmarks.db"
mkdir -p "$INCOMING"

rsync --compress --times "$SOURCE" "$PULLED"

python3 -I - "$PULLED" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
result = connection.execute("PRAGMA quick_check").fetchone()[0]
count = connection.execute("SELECT count(*) FROM bookmarks").fetchone()[0]
connection.close()
if result != "ok":
    sys.exit(f"FAILED: pulled database is damaged: {result}")
print(f"pulled {count} bookmarks")
PY

if [ -d "$REPO/store" ] && [ -x "$REPO/.venv/bin/python" ]; then
    cd "$REPO"
    .venv/bin/python cli.py import --db "$PULLED"
    .venv/bin/python cli.py export
    exit 0
fi

if [ -f "$LOCAL" ]; then
    python3 -I - "$LOCAL" "$PULLED" <<'PY'
import sqlite3, sys
def ids(path: str) -> set[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    found = {row[0] for row in connection.execute("SELECT id FROM bookmarks")}
    connection.close()
    return found
missing = ids(sys.argv[1]) - ids(sys.argv[2])
if missing:
    sys.exit(f"FAILED: pulled copy lacks {len(missing)} bookmarks this machine "
             f"has (e.g. {sorted(missing)[:3]}); bookmarks.db left unchanged")
PY
fi

cp "$PULLED" "$LOCAL.tmp"
mv "$LOCAL.tmp" "$LOCAL"
echo "replaced bookmarks.db with the pulled copy"
