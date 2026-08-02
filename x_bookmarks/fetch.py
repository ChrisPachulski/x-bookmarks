"""Paginated retrieval of the authenticated user's bookmarks.

`GET /2/users/:id/bookmarks` returns at most the 800 most recent bookmarks.
That is an endpoint ceiling, not a client one -- anything older has already
fallen out of reach and only a data archive export can recover it. This is
why the store is append-only: what is captured here is the durable copy.

Pagination is driven manually rather than through tweepy.Paginator so that a
failure on page 7 still returns pages 1-6 instead of losing the whole run.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, TypedDict

from .records import BookmarkRecord, build_page

log = logging.getLogger(__name__)

# Everything needed to rebuild a record without a second call.
TWEET_FIELDS = [
    "id", "text", "created_at", "lang", "author_id",
    "public_metrics", "entities", "attachments", "referenced_tweets",
    # Long-form Articles. Their post text is only a t.co link, so without
    # this field the whole piece -- up to 100,000 characters -- is invisible.
    # It rides along on the bookmark, costing no extra call.
    "article",
]
USER_FIELDS = ["id", "username", "name"]
MEDIA_FIELDS = ["media_key", "type", "url", "preview_image_url", "duration_ms"]
EXPANSIONS = [
    "author_id", "attachments.media_keys",
    # Both halves matter: the first returns the quoted post's text, the second
    # returns who wrote it. Without the second, quoted posts arrive anonymous.
    "referenced_tweets.id", "referenced_tweets.id.author_id",
]

# The posts lookup endpoint takes up to 100 ids per call.
MAX_IDS_PER_LOOKUP = 100

MAX_RESULTS_PER_PAGE = 100
API_WINDOW_LIMIT = 800


###############################################################################
# Client
###############################################################################


def build_client(access_token: str) -> Any:
    """A tweepy client in user context, returning plain dicts.

    return_type=dict keeps the normalization layer working on the API's own
    shapes, so fixtures captured from a real response stay faithful.
    """
    import tweepy

    return tweepy.Client(bearer_token=access_token, return_type=dict)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


###############################################################################
# Pagination
###############################################################################


def fetch_page(client: Any, pagination_token: str | None) -> dict[str, Any]:
    """One page of bookmarks."""
    kwargs: dict[str, Any] = {
        "max_results": MAX_RESULTS_PER_PAGE,
        "tweet_fields": TWEET_FIELDS,
        "user_fields": USER_FIELDS,
        "media_fields": MEDIA_FIELDS,
        "expansions": EXPANSIONS,
    }
    if pagination_token:
        kwargs["pagination_token"] = pagination_token
    return client.get_bookmarks(**kwargs) or {}


def iter_pages(client: Any, max_pages: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield raw pages until exhausted.

    Stops on an empty page or a missing next_token. A page that raises
    propagates -- the caller decides what a partial run means, because only
    it can report that the store is now incomplete.
    """
    token: str | None = None
    page_number = 0

    while True:
        if max_pages is not None and page_number >= max_pages:
            log.info("stopping at page cap (%d)", max_pages)
            return

        page = fetch_page(client, token)
        tweets = page.get("data") or []
        if not tweets:
            return

        page_number += 1
        log.info("page %d: %d bookmarks", page_number, len(tweets))
        yield page

        token = (page.get("meta") or {}).get("next_token")
        if not token:
            return


###############################################################################
# Post lookup -- for X links not already covered by a page's includes
###############################################################################


def fetch_posts(client: Any, ids: list[str]) -> dict[str, dict[str, str]]:
    """Look up posts by id, batched. Returns {id: {text, author_username}}.

    This is how an X permalink gets resolved. Scraping x.com returns the
    page's OG metadata -- in practice a few hundred characters of *reply*
    text, which is not the post and is worse than nothing because it looks
    like content. The API returns the actual post.
    """
    unique = [i for i in dict.fromkeys(ids) if i]
    out: dict[str, dict[str, str]] = {}

    for start in range(0, len(unique), MAX_IDS_PER_LOOKUP):
        batch = unique[start:start + MAX_IDS_PER_LOOKUP]
        try:
            response = client.get_tweets(
                batch, tweet_fields=["id", "text", "author_id"],
                user_fields=USER_FIELDS, expansions=["author_id"],
            ) or {}
        except Exception as exc:
            log.warning("post lookup failed for %d ids: %s", len(batch), exc)
            continue

        users = {
            u["id"]: u
            for u in ((response.get("includes") or {}).get("users") or [])
            if u.get("id")
        }
        for post in (response.get("data") or []):
            author = users.get(str(post.get("author_id") or ""), {})
            out[str(post.get("id"))] = {
                "text": str(post.get("text") or ""),
                "author_username": str(author.get("username") or ""),
            }

        # Deleted, protected, and Article ids come back under `errors` rather
        # than `data`. Absence from the result is the signal callers use.
        missing = set(batch) - set(out)
        if missing:
            log.info("%d of %d ids not returned (deleted, protected, or not a post)",
                     len(missing), len(batch))
    return out


class FetchResult(TypedDict):
    records: list[BookmarkRecord]
    complete: bool          # False when a page failed or the page cap cut the walk short


def fetch_bookmarks(
    access_token: str,
    max_pages: int | None = None,
    known_ids: set[str] | None = None,
    on_page: Any = None,
) -> FetchResult:
    """Every reachable bookmark, normalized into self-contained records.

    When `known_ids` is supplied, stop at the first page that contains
    nothing new. The endpoint has no since_id -- only pagination_token -- so
    incremental has to be done client-side. Bookmarks come back in
    most-recently-bookmarked order, which is what makes an early stop sound:
    once a whole page is already on disk, everything below it is older still.

    Pass known_ids=None to walk every page regardless.

    `on_page` receives each page's records as soon as they are normalized --
    persisting there is what makes a long fetch interruption-safe.

    `complete` is False when a page failed or the page cap stopped the walk:
    an incremental re-run will early-stop before ever reaching what was
    missed, so the caller must say so and point at --full.
    """
    client = build_client(access_token)
    retrieved_at = utc_now()
    records: list[BookmarkRecord] = []
    complete = True
    page_count = 0

    pages = iter_pages(client, max_pages=max_pages)
    while True:
        try:
            page = next(pages)
        except StopIteration:
            break
        except Exception as exc:
            log.error("page %d failed, keeping earlier pages: %s",
                      page_count + 1, exc)
            complete = False
            break

        page_count += 1
        page_records = build_page(
            page.get("data") or [],
            page.get("includes"),
            retrieved_at,
        )
        records.extend(page_records)
        if on_page:
            on_page(page_records)

        if known_ids is not None:
            fresh = [r for r in page_records if r["id"] not in known_ids]
            if not fresh:
                log.info("page held nothing new -- stopping, everything "
                         "below it is older still")
                break

    if max_pages is not None and page_count >= max_pages:
        # The cap may have cut the walk short; treat it as incomplete so the
        # caller warns rather than letting the gap silently age out.
        complete = False

    log.info("retrieved %d bookmarks", len(records))
    if known_ids is None and len(records) >= API_WINDOW_LIMIT:
        log.warning(
            "hit the %d-bookmark API ceiling -- anything older is only "
            "reachable via a data archive export",
            API_WINDOW_LIMIT,
        )
    return {"records": records, "complete": complete}
