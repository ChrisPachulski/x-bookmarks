"""Record shapes and normalization for retrieved bookmarks.

Pure transforms only -- nothing here touches the network or the disk. The X
API returns tweets, expansions, and includes as three loosely-joined blobs;
these functions flatten one page of that into self-contained records so that
every stored file can be read without its siblings.
"""

import logging
import re
from typing import Any, Literal, TypedDict

log = logging.getLogger(__name__)

###############################################################################
# Types
###############################################################################

# Honest status vocabulary, shared with the rest of the project. A record is
# never silently incomplete -- if content is missing, the status says why.
ResolveStatus = Literal["verified", "unresolved", "blocked", "inferred", "pending"]


class LinkRecord(TypedDict):
    """One outbound link found in a bookmark, before or after resolution."""

    short_url: str          # the t.co form as it appears in the tweet
    expanded_url: str       # the destination X itself reports
    display_url: str
    kind: Literal["article", "video", "image", "x_post", "unknown"]
    status: ResolveStatus
    reason: str             # why, when status is not "verified"
    title: str
    text: str               # resolved article body or video transcript


class AuthorRecord(TypedDict):
    id: str
    username: str
    name: str


class MediaRecord(TypedDict):
    media_key: str
    kind: str               # photo | video | animated_gif
    url: str
    preview_image_url: str
    duration_ms: int | None


class ArticleRecord(TypedDict):
    """An X Article (long-form post) carried on the bookmarked post itself.

    The `article` tweet field returns `plain_text` -- the full prose, up to
    100,000 characters. It arrives with the bookmark, so capturing it costs
    nothing beyond asking for the field.
    """

    title: str
    text: str
    preview: str


class ReferencedRecord(TypedDict):
    """A quoted, replied-to, or retweeted post, with its text where known.

    The text arrives free in the same response via the referenced_tweets.id
    expansion. Storing only the id and re-fetching later would pay twice for
    something already on hand.
    """

    type: str               # quoted | replied_to | retweeted
    id: str
    text: str
    author_username: str


class BookmarkRecord(TypedDict):
    """One bookmark, self-contained. This is the on-disk unit."""

    id: str
    text: str
    created_at: str
    lang: str
    author: AuthorRecord
    links: list[LinkRecord]
    media: list[MediaRecord]
    metrics: dict[str, int]
    referenced: list[ReferencedRecord]
    article: ArticleRecord
    resolve_status: ResolveStatus
    retrieved_at: str
    source: str             # which pipeline produced this record


###############################################################################
# Link classification
###############################################################################

_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv",
    "tiktok.com", "dailymotion.com",
)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
_X_HOSTS = ("twitter.com", "x.com", "mobile.twitter.com")

# Both post permalinks and X Article links carry a numeric id the API can
# look up. /i/article/ ids are Articles rather than posts, so a lookup may
# legitimately miss -- that is recorded as unresolved, not silently dropped.
_X_STATUS = re.compile(
    r"(?:twitter|x)\.com/"
    r"(?:i/article/|i/web/status/|[A-Za-z0-9_]+/status(?:es)?/)"
    r"(\d+)",
    re.I,
)


def status_id_from_x_url(url: str) -> str:
    """Numeric post/article id from an X permalink, else empty string."""
    match = _X_STATUS.search(url or "")
    return match.group(1) if match else ""


def is_x_article(url: str) -> bool:
    """True for an X Article link (long-form), as opposed to a post.

    Worth separating because the two fail differently. A post that is missing
    might be deleted or protected and could resolve later; an Article can
    never resolve, because the Articles API is write-only -- create draft and
    publish, with no read endpoint. The API hands back a t.co link and never
    the body, so these are blocked rather than merely unresolved.
    """
    return "/i/article/" in (url or "").lower()


def url_host(url: str) -> str:
    """Lowercased host for a URL, or empty string when unparseable.

    urlsplit().hostname rather than a hand-rolled regex: it strips ports,
    credentials, and IPv6 brackets, all of which a naive authority match
    passes through and misclassifies. Wrapped so malformed API URLs can
    never raise.
    """
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def classify_link(url: str) -> Literal["article", "video", "image", "x_post", "unknown"]:
    """Best-guess link kind, used to route resolution.

    A guess, not a verdict -- resolution corrects it when the fetch reveals
    something different (an "article" that turns out to be a PDF, say).
    """
    host = url_host(url)
    if not host:
        return "unknown"
    if any(host == h or host.endswith(f".{h}") for h in _X_HOSTS):
        return "x_post"
    if any(host == h or host.endswith(f".{h}") for h in _VIDEO_HOSTS):
        return "video"
    path = (url.split("?", 1)[0] or "").lower()
    if path.endswith(_IMAGE_SUFFIXES):
        return "image"
    return "article"


###############################################################################
# Status
###############################################################################


def link_identity(link: dict[str, Any]) -> str:
    """A key for a link that survives resolution.

    Resolution rewrites expanded_url to the post-redirect destination, so
    expanded_url is NOT stable across a re-fetch: the API returns the original
    each time. The t.co short URL is assigned by X and never changes, which
    makes it the only safe key for matching a stored link to a re-fetched one.
    """
    return str(link.get("short_url") or link.get("expanded_url") or "")


def record_status(links: list[dict[str, Any]]) -> str:
    """Roll per-link outcomes into one honest record-level status.

    Always derived, never carried over -- a record whose links include
    unfinished work must not read as finished.
    """
    if not links:
        return "verified"  # nothing to resolve is a complete record
    statuses = {link.get("status") or "pending" for link in links}
    if statuses <= {"verified"}:
        return "verified"
    if "pending" in statuses:
        return "pending"
    if statuses <= {"blocked"}:
        return "blocked"
    if "verified" in statuses:
        return "inferred"
    return "unresolved"


###############################################################################
# Normalization -- one API page into self-contained records
###############################################################################


def index_includes(includes: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Turn the API's `includes` lists into id-keyed lookups.

    The API returns authors and media as flat sibling lists that tweets refer
    to by id. Indexing once per page keeps record building linear rather than
    quadratic.
    """
    includes = includes or {}
    users = {u["id"]: u for u in includes.get("users", []) if u.get("id")}
    media = {
        m["media_key"]: m
        for m in includes.get("media", [])
        if m.get("media_key")
    }
    tweets = {t["id"]: t for t in includes.get("tweets", []) if t.get("id")}
    return {"users": users, "media": media, "tweets": tweets}


def build_author(tweet: dict[str, Any], users: dict[str, dict[str, Any]]) -> AuthorRecord:
    user = users.get(str(tweet.get("author_id") or ""), {})
    return AuthorRecord(
        id=str(user.get("id") or tweet.get("author_id") or ""),
        username=str(user.get("username") or ""),
        name=str(user.get("name") or ""),
    )


def build_links(tweet: dict[str, Any]) -> list[LinkRecord]:
    """Extract outbound links, dropping X's own self-referential media links.

    Every tweet carrying media also carries a t.co link back to itself; those
    are noise for a content pipeline and would each cost a wasted fetch.
    """
    urls = ((tweet.get("entities") or {}).get("urls")) or []
    links: list[LinkRecord] = []
    seen: set[str] = set()
    for entry in urls:
        expanded = str(entry.get("expanded_url") or entry.get("url") or "")
        if not expanded or expanded in seen:
            continue
        kind = classify_link(expanded)
        # A self-link to this same tweet is the media placeholder, not content.
        if kind == "x_post" and f"/status/{tweet.get('id')}" in expanded:
            continue
        seen.add(expanded)
        links.append(LinkRecord(
            short_url=str(entry.get("url") or ""),
            expanded_url=expanded,
            display_url=str(entry.get("display_url") or ""),
            kind=kind,
            status="pending",
            reason="",
            title=str(entry.get("title") or ""),
            text="",
        ))
    return links


def build_media(tweet: dict[str, Any], media: dict[str, dict[str, Any]]) -> list[MediaRecord]:
    keys = ((tweet.get("attachments") or {}).get("media_keys")) or []
    out: list[MediaRecord] = []
    for key in keys:
        item = media.get(key)
        if not item:
            continue
        out.append(MediaRecord(
            media_key=str(key),
            kind=str(item.get("type") or ""),
            url=str(item.get("url") or ""),
            preview_image_url=str(item.get("preview_image_url") or ""),
            duration_ms=item.get("duration_ms"),
        ))
    return out


def build_referenced(
    tweet: dict[str, Any],
    lookups: dict[str, dict[str, Any]],
) -> list[ReferencedRecord]:
    """Quoted/replied-to posts, carrying their text from the same response.

    The referenced_tweets.id expansion already returns these in `includes`.
    Keeping only the id would throw away content we have paid for and force a
    second lookup later.
    """
    tweets = lookups.get("tweets", {})
    users = lookups.get("users", {})
    out: list[ReferencedRecord] = []
    for reference in (tweet.get("referenced_tweets") or []):
        reference_id = str(reference.get("id") or "")
        source = tweets.get(reference_id, {})
        author = users.get(str(source.get("author_id") or ""), {})
        out.append(ReferencedRecord(
            type=str(reference.get("type") or ""),
            id=reference_id,
            text=str(source.get("text") or ""),
            author_username=str(author.get("username") or ""),
        ))
    return out


def build_article(tweet: dict[str, Any]) -> ArticleRecord:
    """Long-form Article body from the post, empty when there is none.

    `plain_text` is the whole piece; `preview_text` is the ~200 character
    teaser. Both are kept -- the preview is what a listing wants and the full
    text is what a reader wants, and they cost the same to store.
    """
    article = tweet.get("article") or {}
    return ArticleRecord(
        title=str(article.get("title") or ""),
        text=str(article.get("plain_text") or ""),
        preview=str(article.get("preview_text") or ""),
    )


def build_record(
    tweet: dict[str, Any],
    lookups: dict[str, dict[str, Any]],
    retrieved_at: str,
) -> BookmarkRecord:
    """One API tweet plus its page's includes into one standalone record."""
    referenced = build_referenced(tweet, lookups)
    return BookmarkRecord(
        id=str(tweet.get("id") or ""),
        text=str(tweet.get("text") or ""),
        created_at=str(tweet.get("created_at") or ""),
        lang=str(tweet.get("lang") or ""),
        author=build_author(tweet, lookups.get("users", {})),
        links=build_links(tweet),
        media=build_media(tweet, lookups.get("media", {})),
        metrics=dict(tweet.get("public_metrics") or {}),
        referenced=referenced,
        article=build_article(tweet),
        resolve_status="pending",
        retrieved_at=retrieved_at,
        source="x_api_v2_bookmarks",
    )


def build_page(
    tweets: list[dict[str, Any]],
    includes: dict[str, Any] | None,
    retrieved_at: str,
) -> list[BookmarkRecord]:
    """Normalize a full page. Malformed tweets are skipped, never fatal."""
    lookups = index_includes(includes)
    records: list[BookmarkRecord] = []
    for tweet in tweets:
        try:
            record = build_record(tweet, lookups, retrieved_at)
        except Exception as exc:
            log.warning("skipped malformed tweet %s: %s",
                        tweet.get("id", "<no id>"), exc)
            continue
        if record["id"]:
            records.append(record)
    return records
