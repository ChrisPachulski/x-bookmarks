# x-bookmarks

Retrieves my X/Twitter bookmarks and the content behind them -- article text,
video transcripts -- and lands it on disk in a stable, greppable format.

**Retrieval only.** Classification, review surfaces, digests, and analysis are
the job of separate skills that read the store this writes. This project's
only job is to make the content exist locally.

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

## Setup

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

### 3. Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 4. Consent (one time)

```bash
.venv/bin/python cli.py auth
```

Open the printed URL, authorize, then paste back the URL your browser was
redirected to. That page will fail to load -- expected, nothing is listening
on that port. The address bar is what matters.

The refresh token is stored at `~/.config/x-bookmarks/token.json` (mode 0600)
and refreshes silently from then on. You only repeat this if you revoke access.

## Usage

```bash
.venv/bin/python cli.py run          # fetch, then resolve content
.venv/bin/python cli.py fetch        # bookmarks only, no content
.venv/bin/python cli.py resolve      # resolve links already stored
.venv/bin/python cli.py status       # what is on disk
```

Useful flags:

```bash
cli.py fetch --max-pages 1      # first 100 bookmarks, for a smoke test
cli.py resolve --limit 20       # resolve a slice at a time
cli.py resolve --all            # re-resolve links already verified
cli.py --store /path/to/store   # somewhere other than ./store
```

A capped or partially failed fetch exits non-zero and says so: an incremental
re-run stops at the first fully known page, so the only way to recover a gap
before it ages out of the 800-bookmark window is `fetch --full`.

Every command is safe to re-run and safe to interrupt. Content is written as
each item completes -- fetch persists page by page, resolve record by record --
so a crash keeps everything retrieved up to that point.

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
| `blocked` | cannot be attempted (image, paywall, auth wall, deleted) |
| `inferred` | partial -- some links resolved, others did not |
| `pending` | not yet attempted |

A record whose links partly failed reads `inferred`, never `verified`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

External services are mocked; transform logic is tested against a fixture
captured from a real API response shape. Stages 2-4 are testable without
credentials, so app registration blocks nothing.
