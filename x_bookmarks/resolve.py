"""Turn bookmarked links into stored content.

A bookmark on its own is a pointer. This stage follows it: articles become
text, videos become transcripts, and anything that cannot be retrieved is
marked with a reason rather than silently dropped.

Nothing here summarizes. Raw text is what gets stored, because a downstream
consumer can always summarize but can never un-summarize.

Every failure is per-item and non-fatal. One dead link must not cost a run.
"""

import logging
import re
from typing import Any

from .records import (
    BookmarkRecord,
    LinkRecord,
    classify_link,
    is_bare_link,
    is_x_article,
    record_status,
    status_id_from_x_url,
    url_host,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
MAX_TEXT_CHARS = 400_000

# Bound on how much of a response body is read. A bookmarked URL is attacker-
# controllable content; an unbounded read lets one link exhaust memory.
MAX_FETCH_BYTES = 25_000_000

# A browser-ish agent: a bare python-requests UA is refused by a meaningful
# share of publishers before any paywall logic runs.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# `v` may sit anywhere in the query string (watch?feature=...&v=...), and
# hosts arrive in mixed case; both forms are real bookmarks.
_YT_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.I,
)


###############################################################################
# Network guards
###############################################################################


def is_private_host(host: str) -> bool:
    """True when the host resolves to a loopback, private, or reserved address.

    A bookmarked URL (or a redirect it takes) can point at 127.0.0.1, the
    link-local metadata range, or a LAN service; fetching those turns the
    resolver into a proxy for probing the local network. Unresolvable hosts
    return False -- the fetch will fail on its own with a clearer reason.
    """
    import ipaddress
    import socket

    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (address.is_private or address.is_loopback
                or address.is_link_local or address.is_reserved):
            return True
    return False


def read_capped(response: Any) -> tuple[bytes, bool]:
    """Read a streamed response up to MAX_FETCH_BYTES. Returns (body, truncated)."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_FETCH_BYTES:
            return b"".join(chunks), True
    return b"".join(chunks), False


###############################################################################
# Link expansion
###############################################################################


def expand_url(url: str) -> tuple[str, str]:
    """Follow redirects to a final URL. Returns (final_url, reason).

    The API's expanded_url is usually the real destination already, but
    newsletter and tracking wrappers add another hop or two that would
    otherwise be stored as the canonical location. One streamed GET rather
    than HEAD-then-GET: it serves HEAD-hostile servers in a single code path,
    and the context manager closes the connection before the body downloads.
    """
    import requests

    try:
        with requests.get(
            url, allow_redirects=True, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT}, stream=True,
        ) as response:
            return str(response.url), ""
    except Exception as exc:
        return url, f"{type(exc).__name__}: {exc}"


###############################################################################
# Article text
###############################################################################


def extract_article(url: str) -> tuple[str, str, str]:
    """Fetch and extract readable article text.

    Returns (title, text, reason). An empty text with an empty reason means
    the page fetched cleanly but held no extractable article body -- which is
    a real outcome for video pages and app shells, not an error.
    """
    import requests

    try:
        with requests.get(
            url, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT}, stream=True,
        ) as response:
            if response.status_code == 402 or response.status_code == 451:
                return "", "", f"paywalled or legally blocked (HTTP {response.status_code})"
            if response.status_code in (401, 403):
                return "", "", f"access denied (HTTP {response.status_code})"
            if response.status_code >= 400:
                return "", "", f"HTTP {response.status_code}"

            content_type = (response.headers.get("content-type") or "").lower()
            is_pdf = ("pdf" in content_type
                      or url.lower().split("?", 1)[0].endswith(".pdf"))
            if not is_pdf and "html" not in content_type and "text" not in content_type:
                return "", "", f"not a text document ({content_type or 'unknown type'})"

            payload, truncated = read_capped(response)
            encoding = response.encoding or "utf-8"
    except Exception as exc:
        return "", "", f"fetch failed: {type(exc).__name__}: {exc}"

    # A bookmarked PDF is a paper or a writeup -- squarely the kind of thing
    # this pipeline exists to capture, so it gets extracted rather than
    # rejected for not being HTML.
    if is_pdf:
        if truncated:
            return "", "", f"PDF larger than the {MAX_FETCH_BYTES} byte cap"
        text, reason = extract_pdf(payload)
        return "", text[:MAX_TEXT_CHARS], reason

    if truncated:
        log.warning("body of %s truncated at %d bytes", url, MAX_FETCH_BYTES)
    html = payload.decode(encoding, errors="replace")
    title, text = extract_from_html(html, url)
    return title, text[:MAX_TEXT_CHARS], ""


def extract_pdf(payload: bytes) -> tuple[str, str]:
    """Text from PDF bytes. Returns (text, reason).

    A scanned PDF yields no text layer and extracts to nothing; that is
    reported as its own reason rather than a generic failure, because the
    remedy is different (OCR, not a retry).
    """
    try:
        import pypdf
    except ImportError:
        return "", "pypdf not installed"

    import io

    try:
        reader = pypdf.PdfReader(io.BytesIO(payload))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        return "", f"PDF parse failed: {type(exc).__name__}: {exc}"

    text = "\n\n".join(page for page in pages if page.strip())
    if not text.strip():
        return "", f"PDF has no text layer ({len(pages)} pages, likely scanned)"
    return text, ""


def extract_from_html(html: str, url: str) -> tuple[str, str]:
    """Trafilatura extraction. Returns (title, text), empty when nothing extracts.

    No lower-quality fallback: whole-page regex strips were being stored as
    "verified" article text when trafilatura found no body. An honest empty
    result becomes unresolved with a reason, which a later re-run can retry.
    """
    try:
        import trafilatura
    except ImportError:
        log.warning("trafilatura not installed -- article extraction disabled")
        return "", ""

    try:
        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=True,
            favor_recall=True, url=url,
        )
        metadata = trafilatura.extract_metadata(html)
        title = getattr(metadata, "title", "") or ""
        return title, extracted or ""
    except Exception as exc:
        log.debug("trafilatura failed on %s: %s", url, exc)
        return "", ""


###############################################################################
# Video transcripts
###############################################################################


def youtube_video_id(url: str) -> str:
    match = _YT_ID.search(url or "")
    return match.group(1) if match else ""


def extract_transcript(url: str) -> tuple[str, str]:
    """Raw YouTube transcript text. Returns (text, reason).

    Deliberately not the `youtube-transcripts` skill: that is an interactive
    NotebookLM/playwright workflow which returns a synthesized overview rather
    than raw text, and its NotebookLM auth is a main-session gate. This needs
    batch-safe raw transcripts, which the library gives directly.
    """
    video_id = youtube_video_id(url)
    if not video_id:
        return "", "not a recognized YouTube URL"

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return "", "youtube-transcript-api not installed"

    try:
        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id)
        except Exception:
            # The English default missed; a transcript in any language is
            # still raw content worth storing over nothing.
            fetched = next(iter(api.list(video_id))).fetch()
        return " ".join(snippet.text for snippet in fetched), ""
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


###############################################################################
# Per-link and per-record resolution
###############################################################################


def resolve_x_post(
    resolved: dict[str, Any],
    url: str,
    post_lookup: dict[str, dict[str, str]],
) -> LinkRecord:
    """Resolve an X permalink from API data only -- never by scraping.

    Fetching x.com returns the page's OG metadata, which in practice is a few
    hundred characters of *reply* text. Storing that as the post's content
    marks a record verified while holding the wrong thing entirely, which is
    worse than an honest failure.
    """
    if is_x_article(url):
        # Reached only when the Article body is not on this record -- i.e. a
        # link to someone else's Article. There is no read endpoint keyed by
        # article id, so this cannot be resolved from the article link alone.
        resolved.update(
            status="blocked",
            reason="Article body not carried on this bookmark; it belongs to "
                   "another post and there is no read endpoint keyed by "
                   "article id",
        )
        return resolved  # type: ignore[return-value]

    status_id = status_id_from_x_url(url)
    if not status_id:
        resolved.update(status="unresolved",
                        reason="X link with no post id in the URL")
        return resolved  # type: ignore[return-value]

    post = post_lookup.get(status_id)
    if not post or not post.get("text"):
        resolved.update(
            status="unresolved",
            reason=f"post {status_id} not returned by the API "
                   "(deleted, protected, or an Article rather than a post)",
        )
        return resolved  # type: ignore[return-value]

    author = post.get("author_username") or ""
    # A post whose text is only a URL is a pointer -- an Article whose body
    # was not returned. Keep the URL, but partial is inferred, never verified.
    bare = is_bare_link(post["text"])
    resolved.update(
        status="inferred" if bare else "verified",
        text=post["text"],
        reason=("post text is only a link; the body behind it was not "
                "returned by the API") if bare else "",
        title=resolved.get("title") or (f"@{author}" if author else ""),
    )
    return resolved  # type: ignore[return-value]


def resolve_link(
    link: LinkRecord,
    post_lookup: dict[str, dict[str, str]] | None = None,
) -> LinkRecord:
    """Resolve one link into content, never raising."""
    resolved = dict(link)
    url = link.get("expanded_url") or link.get("short_url") or ""
    if not url:
        resolved.update(status="blocked", reason="no URL on this link")
        return resolved  # type: ignore[return-value]

    if is_private_host(url_host(url)):
        resolved.update(
            status="blocked",
            reason=f"{url_host(url) or 'URL'} resolves to a private or "
                   "loopback address; refusing to fetch",
        )
        return resolved  # type: ignore[return-value]

    final_url, redirect_reason = expand_url(url)
    if final_url != url:
        resolved["expanded_url"] = final_url
    # The redirect chain itself may land somewhere private; re-check before
    # any content fetch touches the final destination.
    if is_private_host(url_host(final_url)):
        resolved.update(
            status="blocked",
            reason=f"redirects to {url_host(final_url) or 'a private host'}; "
                   "refusing to fetch a private or loopback address",
        )
        return resolved  # type: ignore[return-value]
    # Re-classify: the true kind is only knowable after the redirect resolves.
    kind = classify_link(final_url)
    resolved["kind"] = kind

    if kind == "image":
        resolved.update(status="blocked", reason="image, no text to extract")
        return resolved  # type: ignore[return-value]

    if kind == "x_post":
        return resolve_x_post(resolved, final_url, post_lookup or {})

    if kind == "video":
        text, reason = extract_transcript(final_url)
        if text:
            resolved.update(status="verified", text=text, reason="")
        else:
            host = url_host(final_url)
            resolved.update(
                status="unresolved",
                reason=reason or f"no transcript available from {host}",
            )
        return resolved  # type: ignore[return-value]

    title, text, reason = extract_article(final_url)
    if title and not resolved.get("title"):
        resolved["title"] = title
    if text:
        resolved.update(status="verified", text=text, reason="")
    else:
        resolved.update(
            status="unresolved",
            reason=reason or redirect_reason or "no extractable text",
        )
    return resolved  # type: ignore[return-value]


def post_lookup_from_records(
    records: list[BookmarkRecord],
) -> dict[str, dict[str, str]]:
    """Seed a lookup from quoted-post text already captured at fetch time.

    Most X links in a bookmark are the post it quotes, and that text arrives
    with the bookmark itself. Seeding from it means the API lookup only has
    to cover what is genuinely missing.
    """
    lookup: dict[str, dict[str, str]] = {}
    for record in records:
        for reference in (record.get("referenced") or []):
            text = reference.get("text") or ""
            # A bare link is an Article's post text, not its body. Seeding it
            # would stop the lookup that can return the body.
            if reference.get("id") and text and not is_bare_link(text):
                lookup[reference["id"]] = {
                    "text": reference["text"],
                    "author_username": reference.get("author_username", ""),
                }
    return lookup


def missing_x_post_ids(
    records: list[BookmarkRecord],
    lookup: dict[str, dict[str, str]],
    skip_resolved: bool = True,
) -> list[str]:
    """X post ids still needing an API lookup after seeding."""
    wanted: list[str] = []
    for record in records:
        for link in (record.get("links") or []):
            if link.get("kind") != "x_post":
                continue
            if skip_resolved and link.get("status") == "verified" and link.get("text"):
                continue
            url = link.get("expanded_url") or ""
            # Article ids are not post ids; looking them up returns a
            # guaranteed Not Found and wastes a paid call every run.
            if is_x_article(url):
                continue
            status_id = status_id_from_x_url(url)
            if status_id and status_id not in lookup:
                wanted.append(status_id)
    return list(dict.fromkeys(wanted))


def resolve_record(
    record: BookmarkRecord,
    skip_resolved: bool = True,
    post_lookup: dict[str, dict[str, str]] | None = None,
) -> BookmarkRecord:
    """Resolve every link on one bookmark.

    Already-verified links are left alone by default so re-runs cost nothing
    and cannot degrade content that was retrieved when the source was up.
    """
    links = record.get("links") or []
    if not links:
        return {**record, "resolve_status": "verified"}  # type: ignore[return-value]

    # An Article link on a bookmark points at that bookmark's own long-form
    # body, which the fetch already carried in. Satisfy it from the record
    # rather than sending it down the lookup path, which cannot resolve it.
    article = record.get("article") or {}

    out: list[LinkRecord] = []
    for link in links:
        if skip_resolved and link.get("status") == "verified" and link.get("text"):
            out.append(link)
            continue
        if is_x_article(link.get("expanded_url") or "") and article.get("text"):
            out.append({  # type: ignore[arg-type]
                **link,
                "status": "verified",
                "text": article["text"],
                "title": link.get("title") or article.get("title", ""),
                "reason": "",
            })
            continue
        out.append(resolve_link(link, post_lookup=post_lookup))

    return {  # type: ignore[return-value]
        **record,
        "links": out,
        "resolve_status": record_status(out),
    }


def resolve_records(
    records: list[BookmarkRecord],
    skip_resolved: bool = True,
    on_resolved: Any = None,
    post_lookup: dict[str, dict[str, str]] | None = None,
) -> list[BookmarkRecord]:
    """Resolve a batch, persisting as it goes when a callback is supplied.

    The callback is what makes a long run interruptible: content is on disk
    as soon as it is retrieved, so a crash at item 300 keeps the first 299.
    """
    out: list[BookmarkRecord] = []
    for index, record in enumerate(records, start=1):
        try:
            resolved = resolve_record(record, skip_resolved=skip_resolved,
                                      post_lookup=post_lookup)
        except Exception as exc:
            log.warning("resolve failed for %s: %s", record.get("id"), exc)
            resolved = {**record, "resolve_status": "unresolved"}  # type: ignore[assignment]

        out.append(resolved)
        if on_resolved:
            try:
                on_resolved(resolved)
            except Exception as exc:
                log.warning("persist callback failed for %s: %s",
                            record.get("id"), exc)
        if index % 25 == 0:
            log.info("resolved %d/%d", index, len(records))
    return out
