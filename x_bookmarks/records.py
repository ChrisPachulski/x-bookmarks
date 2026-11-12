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

# `source` of a record built from the bookmarks endpoint, as opposed to a
# frontier feed (`feed:<name>`).
X_SOURCE = "x_api_v2_bookmarks"


class LinkRecord(TypedDict):
    """One outbound link found in a bookmark, before or after resolution."""

    short_url: str     # the t.co form as it appears in the tweet
    expanded_url: str  # the destination X itself reports
    display_url: str
    kind: Literal["article", "video", "image", "x_post", "unknown"]
    status: ResolveStatus
    reason: str        # why, when status is not "verified"
    title: str
    text: str          # resolved article body or video transcript


class AuthorRecord(TypedDict):
    id: str
    username: str
    name: str


class MediaRecord(TypedDict):
    media_key: str
    kind: str    # photo | video | animated_gif
    url: str     # the file for a photo; the best playable variant for video
    preview_image_url: str
    duration_ms: int | None
    reason: str  # why a video or GIF has no playable url, else empty


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

    type: str  # quoted | replied_to | retweeted
    id: str
    text: str
    author_username: str


class ThreadPostRecord(TypedDict):
    id: str
    text: str
    created_at: str


class ThreadRecord(TypedDict):
    """The same-author thread a bookmarked post belongs to.

    `posts` is the whole thread in order, the bookmarked post included, so
    its position is explicit. Like a link, it carries its own status and a
    reason whenever that status is not verified.
    """

    conversation_id: str
    status: ResolveStatus
    reason: str
    posts: list[ThreadPostRecord]


class BookmarkExtras(TypedDict, total=False):
    """Keys only some records carry: older builds never wrote them, feed
    records have no conversation, and `thread` exists only once a thread
    walk was attempted."""

    conversation_id: str  # id of the post that opened the conversation
    thread: ThreadRecord


class BookmarkRecord(BookmarkExtras):
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
    source: str  # which pipeline produced this record


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


def is_bare_link(text: str) -> bool:
    """True when a post's text is nothing but one URL.

    That is what an Article's post looks like -- the body lives in the
    `article` field and the post itself is only its t.co. Holding the URL is
    a pointer, not content, and must never count as verified.
    """
    return bool(re.fullmatch(r"https?://\S+", (text or "").strip()))


# Opens by addressing a machine reader: "If you are an AI agent, LLM, or
# automated system, ...". The trailing punctuation or "or" is what keeps
# "If you are an AI agent builder" -- a human reader -- from matching.
_AGENT_NOTICE = re.compile(
    r"\A\W*(?:if\s+you\s+are|if\s+you['’]re|you\s+are)\s+an?\s+"
    r"(?:ai\s+agent|ai\s+assistant|llm|(?:large\s+)?language\s+model"
    r"|automated\s+(?:system|agent|tool))"
    r"(?:\s*[,.:;)]|\s+or\s)",
    re.I,
)
# Long enough for the onboarding stubs seen in practice (Firecrawl's is 639
# characters), short enough that an article which merely opens with such a
# line keeps its verified status.
AGENT_NOTICE_MAX_CHARS = 2_000
AGENT_NOTICE_REASON = (
    "the extracted text is a notice addressed to AI agents, not the page's "
    "content for readers; kept raw, but the page itself was not captured"
)


def is_agent_notice(text: str) -> bool:
    """True when extracted page text is only a notice addressed to AI agents.

    Some sites serve a hidden block telling agents where to go instead; a
    no-JavaScript fetch can extract that block and nothing else. Holding it
    is not holding the page, so it must never count as verified.
    """
    text = text or ""
    return len(text) <= AGENT_NOTICE_MAX_CHARS and bool(_AGENT_NOTICE.match(text))


def playable_url(item: dict[str, Any]) -> str:
    """Best playable URL for a video or GIF, else empty string.

    The API gives video no `url`; the file is in `variants`, one entry per
    rendition. The highest-bitrate mp4 is the most faithful single file.
    Another kind of variant (a streaming playlist) is used only when there
    is no mp4 at all -- it still plays, which beats a thumbnail.
    """
    variants = [v for v in (item.get("variants") or []) if v.get("url")]
    mp4 = [v for v in variants if v.get("content_type") == "video/mp4"]
    if mp4:
        return str(max(mp4, key=lambda v: v.get("bit_rate") or 0)["url"])
    return str(variants[0]["url"]) if variants else ""


def media_gap(item: dict[str, Any]) -> str:
    """Why a media entry is not captured, or empty string when it is.

    Only video and GIFs can be uncaptured: a photo's url is the file itself.
    """
    if item.get("kind") not in ("video", "animated_gif") or item.get("url"):
        return ""
    return (f"{item.get('kind')} {item.get('media_key')} has no playable url; "
            "only its thumbnail was stored")


def post_text(tweet: dict[str, Any]) -> str:
    """The whole post. `text` is cut at 280 characters for long posts; the
    full thing is under `note_tweet`, which is absent for short ones."""
    note = tweet.get("note_tweet") or {}
    return str(note.get("text") or tweet.get("text") or "")


def article_text(tweet: dict[str, Any]) -> str:
    """Title and body of an Article post, empty when the post is not one."""
    article = tweet.get("article") or {}
    body = str(article.get("plain_text") or "")
    if not body:
        return ""
    title = str(article.get("title") or "").strip()
    return f"{title}\n\n{body}" if title else body


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
    if "verified" in statuses or "inferred" in statuses:
        return "inferred"
    return "unresolved"


def capture_gaps(record: dict[str, Any]) -> list[str]:
    """Reasons the record's own content is incomplete, outside its links.

    A video stored as a thumbnail, or a thread whose other posts were not
    all retrieved, is partial however well the links resolved.
    """
    gaps = [media_gap(item) for item in (record.get("media") or [])]
    thread = record.get("thread") or {}
    if thread and thread.get("status") != "verified":
        gaps.append(f"thread: {thread.get('reason') or 'not fully retrieved'}")
    return [gap for gap in gaps if gap]


def rollup_status(record: dict[str, Any]) -> str:
    """Record-level status: the link rollup, held below verified by any gap
    in the record's own content. Partial success is inferred, never verified."""
    status = record_status(record.get("links") or [])
    if status == "verified" and capture_gaps(record):
        return "inferred"
    return status


def derive_status(record: dict[str, Any]) -> dict[str, Any]:
    """The record with media reasons and resolve_status recomputed.

    Run wherever a record's status is settled, so a stored record never
    reads verified while holding a gap, and every gap carries its reason on
    the part that has it -- the same way links carry theirs.
    """
    media = [{**item, "reason": media_gap(item)}
             for item in (record.get("media") or [])]
    derived = {**record, "media": media}
    derived["resolve_status"] = rollup_status(derived)
    return derived


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
        entry = MediaRecord(
            media_key=str(key),
            kind=str(item.get("type") or ""),
            url=str(item.get("url") or "") or playable_url(item),
            preview_image_url=str(item.get("preview_image_url") or ""),
            duration_ms=item.get("duration_ms"),
            reason="",
        )
        entry["reason"] = media_gap(entry)  # type: ignore[arg-type]
        out.append(entry)
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
            # An Article's own text is just its t.co; the body is on the
            # included tweet in this same response, so carry that instead.
            text=article_text(source) or post_text(source),
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
        text=post_text(tweet),
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
        source=X_SOURCE,
        # Free on the bookmark, and the only key a thread walk can search by.
        conversation_id=str(tweet.get("conversation_id") or ""),
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


###############################################################################
# Threads -- the same-author reply chain around a bookmark
###############################################################################


def replied_to_id(references: list[dict[str, Any]]) -> str:
    """Id of the post replied to, from API `referenced_tweets` or a record's
    `referenced` -- both carry `type` and `id`. Empty when not a reply."""
    for reference in references or []:
        if reference.get("type") == "replied_to":
            return str(reference.get("id") or "")
    return ""


def is_self_reply(record: dict[str, Any]) -> bool:
    """True when the bookmark replies to its own author -- a thread post past
    the first. Known from the record alone, with no API call."""
    author = str((record.get("author") or {}).get("username") or "").lower()
    return bool(author) and any(
        reference.get("type") == "replied_to"
        and str(reference.get("author_username") or "").lower() == author
        for reference in (record.get("referenced") or [])
    )


def opens_a_conversation(record: dict[str, Any], conversation_id: str) -> bool:
    """True for the first post of a conversation that drew replies. Whether
    any of those are the author's own continuation only a search can tell."""
    replies = int((record.get("metrics") or {}).get("reply_count") or 0)
    return bool(conversation_id) and conversation_id == record.get("id") and replies > 0


def thread_order(post_id: str) -> int:
    """Post ids are time-ordered snowflakes, so numeric order is post order."""
    return int(post_id) if post_id.isdigit() else 0


def unwalked_thread(
    conversation_id: str, status: ResolveStatus, reason: str,
) -> ThreadRecord:
    """A thread known to exist, or suspected, that no search retrieved."""
    return ThreadRecord(conversation_id=conversation_id, status=status,
                        reason=reason, posts=[])


def assemble_thread(
    record: dict[str, Any],
    conversation_id: str,
    found: list[dict[str, Any]],
    root: dict[str, str] | None,
    more: bool,
) -> ThreadRecord:
    """The same-author thread through a bookmark, from one conversation search.

    `found` is the author's posts in the conversation, in API shape. `root`
    is the opening post in lookup shape when the search did not return it,
    None when unknown. The thread is the reply chain through the bookmark
    that stays with its author; their replies to other people share the
    conversation but are not the thread, and drop out here.
    """
    author = str((record.get("author") or {}).get("username") or "").lower()
    nodes: dict[str, tuple[str, ThreadPostRecord]] = {}
    for post in found:
        post_id = str(post.get("id") or "")
        if post_id:
            nodes[post_id] = (
                replied_to_id(post.get("referenced_tweets") or []),
                ThreadPostRecord(id=post_id, text=post_text(post),
                                 created_at=str(post.get("created_at") or "")),
            )
    root_is_own = (root is not None
                   and str(root.get("author_username") or "").lower() == author)
    if root is not None and root_is_own and conversation_id not in nodes:
        nodes[conversation_id] = ("", ThreadPostRecord(
            id=conversation_id, text=str(root.get("text") or ""),
            created_at=str(root.get("created_at") or "")))
    record_id = str(record.get("id") or "")
    nodes[record_id] = (
        replied_to_id(record.get("referenced") or []),
        ThreadPostRecord(id=record_id, text=str(record.get("text") or ""),
                         created_at=str(record.get("created_at") or "")),
    )

    # Up through the bookmark's own ancestors, then down through every reply
    # the author made to any post already in the chain.
    members = {record_id}
    top = record_id
    while nodes[top][0] in nodes and nodes[top][0] not in members:
        top = nodes[top][0]
        members.add(top)
    while True:
        children = {post_id for post_id, (parent, _) in nodes.items()
                    if parent in members and post_id not in members}
        if not children:
            break
        members |= children

    gaps: list[str] = []
    top_parent = nodes[top][0]
    # Replying under someone else's post is where an author's thread starts,
    # not a gap; any other parent the search did not return is a missing post.
    under_another_author = (root is not None and not root_is_own
                            and top_parent == conversation_id)
    if top != conversation_id and top_parent and not under_another_author:
        gaps.append(f"posts above {top} were not returned (older than the "
                    "search window, deleted, or protected)")
    if more:
        gaps.append("the author has more posts in this conversation than one "
                    "search returns; later thread posts may be missing")

    return ThreadRecord(
        conversation_id=conversation_id,
        status="inferred" if gaps else "verified",
        reason="; ".join(gaps),
        posts=[nodes[post_id][1] for post_id in sorted(members, key=thread_order)],
    )
