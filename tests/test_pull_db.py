"""pull-db.sh on a machine with no store: replace only on a superset, never
lose a bookmark the local copy already has. rsync takes local paths, so no
network is touched."""

import shutil
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pull-db.sh"


###############################################################################
# Helpers
###############################################################################


def make_db(path: Path, ids: list[str]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE bookmarks (id TEXT PRIMARY KEY, record TEXT)")
    connection.executemany("INSERT INTO bookmarks VALUES (?, '{}')", [(i,) for i in ids])
    connection.commit()
    connection.close()
    return path


def db_ids(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    found = {row[0] for row in connection.execute("SELECT id FROM bookmarks")}
    connection.close()
    return found


def consumer_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    shutil.copy(SCRIPT, repo / "pull-db.sh")
    return repo


def pull(repo: Path, source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(repo / "pull-db.sh"), str(source)],
                          capture_output=True, text=True)


###############################################################################
# Tests
###############################################################################


def test_pull_replaces_local_db_when_pulled_copy_is_a_superset(tmp_path: Path) -> None:
    repo = consumer_repo(tmp_path)
    make_db(repo / "bookmarks.db", ["1", "2"])
    source = make_db(tmp_path / "peer.db", ["1", "2", "3"])

    result = pull(repo, source)

    assert result.returncode == 0, result.stderr
    assert db_ids(repo / "bookmarks.db") == {"1", "2", "3"}


def test_pull_refuses_and_keeps_local_db_when_pulled_copy_lacks_a_bookmark(tmp_path: Path) -> None:
    repo = consumer_repo(tmp_path)
    make_db(repo / "bookmarks.db", ["1", "2", "only-here"])
    source = make_db(tmp_path / "peer.db", ["1", "2", "3"])

    result = pull(repo, source)

    assert result.returncode != 0
    assert "lacks 1 bookmarks" in result.stderr
    assert db_ids(repo / "bookmarks.db") == {"1", "2", "only-here"}


def test_pull_creates_local_db_when_none_exists(tmp_path: Path) -> None:
    repo = consumer_repo(tmp_path)
    source = make_db(tmp_path / "peer.db", ["1"])

    result = pull(repo, source)

    assert result.returncode == 0, result.stderr
    assert db_ids(repo / "bookmarks.db") == {"1"}


def test_pull_without_a_source_fails_before_touching_anything(tmp_path: Path) -> None:
    repo = consumer_repo(tmp_path)

    result = subprocess.run(["sh", str(repo / "pull-db.sh")], capture_output=True,
                            text=True, env={"PATH": "/usr/bin:/bin"})

    assert result.returncode == 2
    assert not (repo / ".incoming").exists()
