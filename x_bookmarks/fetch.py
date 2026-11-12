"""Paginated retrieval of the authenticated user's bookmarks.

`GET /2/users/:id/bookmarks` returns at most the 800 most recent bookmarks.
That is an endpoint ceiling, not a client one -- anything older has already
fallen out of reach and only a data archive export can recover it. This is
why the store is append-only: what is captured here is the durable copy.

Pagination is driven manually rather than through tweepy.Paginator so that a
failure on page 7 still returns pages 1-6 instead of losing the whole run.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, TypedDict

from .records import (
    BookmarkRecord,
    ThreadRecord,
    X_SOURCE,
    article_text,
    assemble_thread,
    build_page,
    is_self_reply,
    opens_a_conversation,
    post_text,
    unwalked_thread,
)

log = logging.getLogger(__name__)

# Everything needed to rebuild a record without a second call.
TWEET_FIELDS = [
    "id", "text", "created_at", "lang", "author_id",
    "public_metrics", "entities", "attachments", "referenced_tweets",
    # Long-form Articles. Their post text is only a t.co link, so without
    # this field the whole piece -- up to 100,000 characters -- is invisible.
    # It rides along on the bookmark, costing no extra call.
    "article",
    # Posts over 280 characters arrive truncated in `text`; the whole post
    # is under `note_tweet`. Without it every long post is stored cut off.
    "note_tweet",
    # The id of the post that opened the conversation. A thread walk
    # searches by it, and only the bookmark carries it for free.
    "conversation_id",
]
USER_FIELDS = ["id", "username", "name"]
MEDIA_FIELDS = [
    "media_key", "type", "url", "preview_image_url", "duration_ms",
    # Video and GIFs have no `url`; their playable files are the variants.
    # Without this field a native video is stored as a thumbnail.
    "variants",
]
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
    """Look up posts by id, batched.

    Returns {id: {text, author_username, conversation_id, created_at}}; the
    last two cost nothing extra and are what a thread walk needs.

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
                batch, tweet_fields=["id", "text", "author_id", "note_tweet",
                                     "article", "conversation_id", "created_at"],
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
                "text": article_text(post) or post_text(post),
                "author_username": str(author.get("username") or ""),
                "conversation_id": str(post.get("conversation_id") or ""),
                "created_at": str(post.get("created_at") or ""),
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
    complete: bool  # False when a page failed or the page cap cut the walk short


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


###############################################################################
# Threads -- one recent search per conversation, capped
###############################################################################

# Recent search reaches back seven days. Full-archive search reaches further
# but is limited to pay-per-use and Enterprise access, which this project
# cannot confirm from here, so it is not used.
SEARCH_WINDOW_DAYS = 7
# One page per conversation, never paginated. Billing is per post returned,
# so this is also the ceiling on what one thread can cost.
THREAD_SEARCH_MAX_RESULTS = 100
# Searches per run: bounds a run that meets many new conversations at once.
MAX_THREAD_SEARCHES = 50
THREAD_TWEET_FIELDS = [
    "id", "text", "created_at", "author_id", "conversation_id",
    "referenced_tweets", "note_tweet",
]


class ThreadWalk(TypedDict):
    threads: dict[str, ThreadRecord]  # bookmark id -> its thread
    searches: int                     # recent-search requests made
    lookups: int                      # post ids sent to the lookup endpoint


def within_search_window(created_at: str, now: datetime) -> bool:
    """True when a post is recent enough for recent search to reach it.

    An unparseable date is attempted rather than skipped; X's billing docs
    charge only for responses that return data.
    """
    try:
        posted = datetime.fromisoformat((created_at or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return now - posted <= timedelta(days=SEARCH_WINDOW_DAYS)


def search_conversation(
    client: Any, conversation_id: str, username: str,
) -> tuple[list[dict[str, Any]], bool]:
    """The author's posts in one conversation, and whether more exist than
    one page holds. Raises on a failed request; the caller records why."""
    response = client.search_recent_tweets(
        query=f"conversation_id:{conversation_id} from:{username}",
        max_results=THREAD_SEARCH_MAX_RESULTS,
        tweet_fields=THREAD_TWEET_FIELDS,
    ) or {}
    meta = response.get("meta") or {}
    return list(response.get("data") or []), bool(meta.get("next_token"))


def fetch_threads(
    client: Any,
    records: list[BookmarkRecord],
    now: datetime | None = None,
    limit: int = MAX_THREAD_SEARCHES,
) -> ThreadWalk:
    """Walk the same-author thread around each bookmark that is part of one.

    Finding candidates costs nothing: a reply to its own author is a thread
    post, and the opening post of a conversation with replies may start one.
    Each candidate inside the search window costs one recent search, plus
    one batched lookup when the conversation's opening post did not come
    back. Records stored before conversation_id was requested cost one
    batched lookup each, and only inside the window.

    A self-reply older than the window is marked blocked without a call. An
    older opening post is left alone: replies alone do not make it a thread.
    One failed conversation is recorded on its own thread and never raises.
    """
    now = now or datetime.now(timezone.utc)
    bookmarks = [r for r in records if r.get("source") == X_SOURCE and r.get("id")]
    in_window = {
        r["id"] for r in bookmarks
        if within_search_window(str(r.get("created_at") or ""), now)
    }
    unknown = list(dict.fromkeys(
        r["id"] for r in bookmarks
        if r["id"] in in_window and not r.get("conversation_id")
    ))
    looked_up = fetch_posts(client, unknown) if unknown else {}

    threads: dict[str, ThreadRecord] = {}
    searched: list[tuple[BookmarkRecord, str, list[dict[str, Any]], bool]] = []
    searches = 0
    for record in bookmarks:
        record_id = record["id"]
        conversation_id = str(record.get("conversation_id")
                              or (looked_up.get(record_id) or {}).get("conversation_id")
                              or "")
        self_reply = is_self_reply(record)
        if not (self_reply or opens_a_conversation(record, conversation_id)):
            continue
        if record_id not in in_window:
            if self_reply:
                threads[record_id] = unwalked_thread(
                    conversation_id, "blocked",
                    f"part of a same-author thread, but older than the "
                    f"{SEARCH_WINDOW_DAYS}-day reach of recent search; only "
                    "the replied-to post was captured")
            continue
        if not conversation_id:
            threads[record_id] = unwalked_thread(
                "", "unresolved",
                "conversation id not returned by the API (deleted, "
                "protected, or the lookup failed)")
            continue
        if searches >= limit:
            log.info("thread search cap (%d) reached; %s left for a later "
                     "`threads` run", limit, record_id)
            continue
        searches += 1
        username = str((record.get("author") or {}).get("username") or "")
        try:
            found, more = search_conversation(client, conversation_id, username)
        except Exception as exc:
            threads[record_id] = unwalked_thread(
                conversation_id, "unresolved",
                f"thread search failed: {type(exc).__name__}: {exc}")
            continue
        searched.append((record, conversation_id, found, more))

    # The opening post is fetched only when it is not the bookmark itself and
    # the search did not return it -- older than the window, or another
    # author's post that this author's thread hangs under.
    roots_wanted = list(dict.fromkeys(
        conversation_id
        for record, conversation_id, found, _ in searched
        if conversation_id != record["id"]
        and conversation_id not in {str(post.get("id")) for post in found}
    ))
    roots = fetch_posts(client, roots_wanted) if roots_wanted else {}
    for record, conversation_id, found, more in searched:
        threads[record["id"]] = assemble_thread(
            record, conversation_id, found, roots.get(conversation_id), more)

    lookups = len(unknown) + len(roots_wanted)
    log.info("threads: %d searches, %d posts looked up, %d threads recorded",
             searches, lookups, len(threads))
    return ThreadWalk(threads=threads, searches=searches, lookups=lookups)
