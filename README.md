<h1 align="center">x-bookmarks</h1>

<p align="center"><em>A bookmark is a promise to your future self. X breaks it after 800.</em></p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="X API v2 user context" src="https://img.shields.io/badge/X_API-v2_user_context-black">
  <img alt="append-only store" src="https://img.shields.io/badge/store-append--only-2ea44f">
  <img alt="tests: pytest" src="https://img.shields.io/badge/tests-pytest-yellow">
</p>

Every bookmark you save on X rides a conveyor belt. The API returns only the
**800 most recent**; past that, a bookmark is still saved -- and permanently
unreachable. The thread you kept to read later is exactly the one that won't
be there when later comes.

**x-bookmarks** is a retrieval pipeline that gets there first: it pulls your
bookmarks and the content behind them -- article text, video transcripts,
quoted posts -- and lands everything on disk in a stable, greppable format.

**Retrieval only.** Classification, review surfaces, digests, and analysis are
the job of separate skills that read the store this writes. This project's
only job is to make the content exist locally.

## How it works

```
fetch ────────────> resolve ────────────> export
bookmark pages      links become text     committed SQLite copy
      └─────────── store/  (JSON, append-only) ───────────┘
```

Three stages, one command: `fetch` pulls bookmark pages from the API,
`resolve` follows each link into stored text, `export` rewrites the committed
`bookmarks.db`. `run` does all three.

## Features

- **Content, not pointers** -- articles become text, PDFs are extracted,
  YouTube links become raw transcripts, and X permalinks resolve through the
  API -- never by scraping, which returns reply text masquerading as the post.
- **Append-only store** -- no code path deletes a record, truncates stored
  content, or lets a re-fetch downgrade a verified link. Re-running only adds
  or enriches.
- **Honest statuses** -- every record and link says what happened:
  `verified`, `unresolved`, `blocked`, `inferred`, or `pending`, with a
  populated `reason` whenever it is not `verified`.
- **Interruption-safe** -- content is written as each item completes, page by
  page and record by record. A crash or Ctrl-C keeps everything retrieved so
  far.
- **Travels with the repo** -- the store is exported to a committed
  `bookmarks.db`, so other machines get the full content by `git pull`
  instead of re-fetching against the sliding window.
- **Raw text, never summaries** -- a consumer can always summarize, but can
  never un-summarize.

## Frontier sources

The same pipeline also retrieves sources that are not bookmarks: the
places where harness and agent engineering is actually published. They
are listed in `feeds.json` and land in the same store, marked
`source: feed:<name>`, so `resolve` and `export` treat them like any other
record.

| kind | what it is | example |
|---|---|---|
| `rss` | an RSS or Atom URL (GitHub release feeds are Atom) | `anthropics/claude-code/releases.atom` |
| `index` | a listing page whose links under `prefix` are the items, for sites with no feed | `anthropic.com/engineering` |
| `page` | one living page, stored again whenever its text changes | the Claude API release notes |
| `arxiv` | an arXiv API query, newest first; the link is the PDF so the full paper is captured | `all:"agent harness"` |
| `last30days` | a topic run through the last30days engine; every item it returns is a record | `AI agent harness engineering` |

```bash
.venv/bin/python cli.py feeds                       # every source
.venv/bin/python cli.py feeds --skip-kind last30days  # feeds only, ~15 s
.venv/bin/python cli.py feeds --only arxiv-agent-harness
.venv/bin/python cli.py resolve && .venv/bin/python cli.py export
```

`defaults` in `feeds.json` window each source to the last 45 days and 50
items (archive feeds serve their whole history); items with no date, such
as index-page links, are always kept. Retrieval stops there: ranking and
the weekly brief are the `frontier-scan` skill's job, reading `bookmarks.db`.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py auth   # one-time consent -- setup details below
.venv/bin/python cli.py run    # fetch, then resolve, then export
```

<details>
<summary><strong>First-run setup: register an app and store the client id</strong></summary>

### 1. Register an app (one time, manual)

At [developer.x.com](https://developer.x.com):

- Create a project and app, then enable **User authentication settings**.
- App permissions: **Read**.
- Type of App: **Native App** (public client, so no client secret needed).
- Callback URI: `http://127.0.0.1:8765/callback`
- Website URL: anything you own.

Copy the **OAuth 2.0 Client ID**.

Bookmarks require OAuth 2.0 user context with the `bookmark.read`,
`tweet.read`, and `users.read` scopes. An app-only bearer token will not work,
no matter which tier it came from.

As of February 2026 X defaults new developers to pay-per-use; reading your own
account data is roughly $0.001 per resource. Confirm current rates against
your own dashboard.

### 2. Store the client id

```bash
mkdir -p ~/.config/x-bookmarks
printf '{"client_id": "YOUR_CLIENT_ID"}' > ~/.config/x-bookmarks/app.json
```

Or set `X_BOOKMARKS_CLIENT_ID` in the environment. Env wins over the file.

If you registered a confidential client instead of a native app, add
`"client_secret"` alongside it.

</details>

<details>
<summary><strong>The consent flow, and why the redirect "fails"</strong></summary>

```bash
.venv/bin/python cli.py auth
```

Open the printed URL, authorize, then paste back the URL your browser was
redirected to. That page will fail to load -- expected, nothing is listening
on that port. The address bar is what matters.

The refresh token is stored at `~/.config/x-bookmarks/token.json` (mode 0600)
and refreshes silently from then on. You only repeat this if you revoke access.

</details>

Here it is after a full run:

```
$ cli.py status
store:     ~/x-bookmarks/store
bookmarks: 198

records by status:
  verified     195
  unresolved   2
  inferred     1

links by status:
  verified     121
  unresolved   3

links with stored content: 121
```

## Commands

```bash
.venv/bin/python cli.py run          # fetch, then resolve content
.venv/bin/python cli.py fetch        # bookmarks only, no content
.venv/bin/python cli.py resolve      # resolve links already stored
.venv/bin/python cli.py status       # what is on disk
.venv/bin/python cli.py export       # rebuild bookmarks.db from the store
```

Useful flags:

```bash
cli.py fetch --max-pages 1      # first 100 bookmarks, for a smoke test
cli.py resolve --limit 20       # resolve a slice at a time
cli.py resolve --all            # re-resolve links already verified
cli.py resolve --repair         # one-time: reset X-post links stored by the
                                # old scraping path, re-resolve via the API
cli.py --store /path/to/store   # somewhere other than ./store
```

A capped or partially failed fetch exits non-zero and says so: an incremental
re-run stops at the first fully known page, so the only way to recover a gap
before it ages out of the 800-bookmark window is `fetch --full`.

Every command is safe to re-run and safe to interrupt. Content is written as
each item completes -- fetch persists page by page, resolve record by record --
so a crash keeps everything retrieved up to that point.

## The 800-bookmark ceiling

`GET /2/users/:id/bookmarks` returns at most the **800 most recent** bookmarks.
That is an endpoint limit, not a client one.

The practical consequence: this store is the only durable record beyond 800.
As new bookmarks accumulate the API window slides forward and older items
become permanently unreachable. So the store is append-only -- a record that
has aged out of the API is never removed, and re-running can only add or
enrich, never destroy.

Bookmarks already older than the window when you first run this are not
recoverable here. A data archive export from X is the only route to those.

## Store layout

```
store/
  bookmarks/<id>.json   one self-contained record per bookmark
  manifest.jsonl        append-only index, one line per bookmark
bookmarks.db            committed SQLite copy, rebuilt by `run` and `export`
```

`bookmarks.db` is a derived artifact: two tables (`bookmarks`, `links`) for
querying, with each full record preserved in a JSON column. It exists so other
machines get the content via `git pull` instead of re-fetching against the
API's sliding window. Regenerate it; never hand-edit it.

### Record shape

Each record is one `BookmarkRecord`: `id`, `text`, `created_at`, `lang`,
`author {id, username, name}`, `links`, `media`, `metrics`, `referenced`,
`article`, `resolve_status`, `retrieved_at`, and `source`
(`x_bookmarks/records.py:78`). The fields that carry retrieved content:

- `links[]` -- one entry per outbound link. `short_url` is the t.co form and
  is a link's stable identity: resolution rewrites `expanded_url` to the
  post-redirect destination while the API keeps returning the original, so
  only `short_url` safely matches a stored link to a re-fetched one
  (`records.py:281`). `text` holds the resolved content -- article body or
  video transcript, depending on `kind` -- and `reason` says why whenever
  `status` is not `verified`.
- `article` -- when the bookmarked post is an X Article (long-form), its full
  body rides along on the bookmark itself: `text` is the whole piece, up to
  100,000 characters, and `preview` is the short teaser. Both arrive with the
  fetch at no extra API cost (`records.py:51`, `fetch.py:21`).
- `referenced[]` -- quoted, replied-to, or retweeted posts, each carrying its
  `text` and `author_username` where the API supplied them in the same
  response (`records.py:64`). When the quoted post is itself an Article,
  `text` is the Article's title and body, not the bare t.co the post carries.
- `text` on the record, and on every `referenced[]` entry, is the whole post.
  The API truncates `text` at 280 characters and puts the full post under
  `note_tweet`; the fetch asks for both and stores the long one
  (`records.py`, `post_text`).

Reading it downstream:

```python
import json, glob
for path in glob.glob("store/bookmarks/*.json"):
    record = json.load(open(path))
    for link in record["links"]:
        if link["status"] == "verified":
            print(record["id"], link["kind"], len(link["text"]))
```

Records are stored raw and never summarized -- a consumer can always
summarize, but can never un-summarize.

## Status vocabulary

Every record and link carries an honest status:

| status | meaning |
|---|---|
| `verified` | content retrieved and confirmed |
| `unresolved` | fetch attempted and failed; `reason` says why |
| `blocked` | cannot be attempted (image, private-address host, another author's X Article) |
| `inferred` | partial -- some links resolved, others did not |
| `pending` | not yet attempted |

A record whose links partly failed reads `inferred`, never `verified`.
A record with no links at all also reads `verified` -- nothing to resolve is
a complete record (`x_bookmarks/records.py:194`).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

External services are mocked; transform logic is tested against a fixture
captured from a real API response shape. The whole suite runs without
credentials, so app registration blocks nothing.
