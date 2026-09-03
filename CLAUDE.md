# x-bookmarks -- project rules

## Scope

Retrieval only. This project fetches bookmarks and the content behind them,
and writes them to disk. Nothing else.

Do NOT add here: classification, tagging, summarization, scoring, dashboards,
digests, or tool-discovery analysis. Those are separate skills that read the
store. Scope creep into any of them is a defect, not a feature.

## Frontier sources are in scope

`feeds.json` and `x_bookmarks/feeds.py` retrieve non-bookmark sources (feeds,
index pages, arXiv, last30days runs) into the same store as `feed:<name>`
records. That is retrieval and belongs here. What is read from them, ranked,
or written up is not: the `frontier-scan` skill in claude-sync reads
`bookmarks.db` for that. The recency window in `feeds.json` is a retrieval
cap on archive-sized feeds, not a judgment about relevance.

## Hard invariants

- **The store is append-only.** The API returns only the 800 most recent
  bookmarks, so this store is the only durable record beyond that. No code
  path may delete a record, truncate stored content, or overwrite verified
  link text with an empty re-fetch.
- **A re-fetch must never downgrade a settled status.** Re-fetched records
  always arrive `pending`; letting that overwrite `verified` reports a
  finished record as unfinished. Guarded in `store.merge_record` and tested.
- **Never summarize on the way in.** Raw text only. A consumer can summarize;
  it cannot un-summarize what was discarded.
- **Resolution failures are per-item and non-fatal.** One dead link must not
  cost a run. Mark the item with a reason and continue.

## Status vocabulary

`verified` / `unresolved` / `blocked` / `inferred` / `pending`. Partial
success is `inferred`, never `verified`. Every non-verified status carries a
populated `reason`.

## Auth

Bookmarks need OAuth 2.0 user context (`bookmark.read`, `tweet.read`,
`users.read`, plus `offline.access` for refresh). App-only bearer tokens are
rejected by the endpoint -- do not "fix" a 401 by trying one.

Tokens live at `~/.config/x-bookmarks/token.json`, mode 0600. Never commit a
token or a client secret. Retrieved content IS committed, but only as the
derived `bookmarks.db` (decided 2026-08-02; the repo is private and must stay
private). The JSON store itself stays untracked.

## Not the youtube-transcripts skill

Video transcripts come from `youtube-transcript-api`, not the
`youtube-transcripts` skill. That skill is an interactive NotebookLM /
playwright workflow returning a synthesized overview, and its NotebookLM auth
is a main-session gate agents cannot pass. This pipeline needs batch-safe raw
text. Do not "consolidate" these.

## Code conventions

Inherits the parent project's Python rules:

- No `main()` functions, no `if __name__` guards. Functions at the top,
  called sequentially below.
- No classes. TypedDict for structured records.
- Type hints on all signatures; `|` for unions, not `Union`.
- `###############` section headers.
- `logging`, not `print()`, outside the CLI presentation layer.
- No emojis, anywhere.

## Tests

pytest, one behavior per test, named for the behavior. Network is mocked.
The append-only guarantee has dedicated tests -- if you touch
`store.merge_record`, they are the ones that matter.

Unit tests alone are not sufficient here. The status-downgrade bug passed 73
mocked tests and was only caught by an end-to-end run against live network.
Run a real fetch-and-resolve before calling a change done.
