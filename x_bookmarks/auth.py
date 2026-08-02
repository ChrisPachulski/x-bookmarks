"""OAuth 2.0 Authorization Code Flow with PKCE for the X API.

Bookmarks are a user-context endpoint: `GET /2/users/:id/bookmarks` requires
an OAuth2 user token and explicitly rejects app-only bearer tokens. So this
runs a real consent flow once, then keeps the resulting refresh token alive.

Consent is paste-the-URL rather than a loopback callback server. It is a
once-ever operation, it works over SSH and on a headless box, and it avoids
standing up an HTTP listener just to catch a single redirect.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "x-bookmarks"
APP_PATH = CONFIG_DIR / "app.json"
TOKEN_PATH = CONFIG_DIR / "token.json"

TOKEN_URL = "https://api.x.com/2/oauth2/token"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"

# offline.access is what makes the refresh token appear; without it consent
# would have to be repeated every couple of hours.
SCOPES = ["bookmark.read", "tweet.read", "users.read", "offline.access"]

# Refresh this far before nominal expiry so a long resolve stage cannot have
# the token die underneath it mid-run.
EXPIRY_MARGIN_SECONDS = 300


###############################################################################
# Loopback transport
###############################################################################

# Hosts where plain HTTP is legitimate for a redirect. RFC 8252 section 7.3
# has native apps receive the authorization code on the loopback interface,
# and loopback traffic never leaves the machine.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def is_loopback_redirect(redirect_uri: str) -> bool:
    """True for an http:// redirect aimed at this machine.

    urlsplit rather than hand-splitting: a bracketed IPv6 authority like
    [::1]:8765 cannot be separated from its port by counting colons, and
    .hostname strips the brackets and lowercases for free.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(redirect_uri or "")
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def allow_loopback_http(redirect_uri: str) -> bool:
    """Permit http:// for loopback redirects only.

    oauthlib refuses any non-https redirect outright, which is right for a
    hosted callback and wrong for a native app: RFC 8252 specifies exactly
    this loopback flow, and there is no way to serve https on 127.0.0.1
    without a certificate no one will trust. Scoped to loopback so a real
    insecure redirect still raises InsecureTransportError.
    """
    if not is_loopback_redirect(redirect_uri):
        return False
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    log.debug("permitting http for loopback redirect %s", redirect_uri)
    return True


###############################################################################
# App credentials
###############################################################################


def load_app_config() -> dict[str, str]:
    """Client id/secret/redirect from env first, then the config file.

    Env wins so a throwaway or second app can be used without editing files.
    """
    config: dict[str, str] = {}
    if APP_PATH.exists():
        try:
            config = json.loads(APP_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("unreadable app config %s: %s", APP_PATH, exc)

    client_id = os.environ.get("X_BOOKMARKS_CLIENT_ID") or config.get("client_id", "")
    client_secret = (
        os.environ.get("X_BOOKMARKS_CLIENT_SECRET")
        or config.get("client_secret", "")
    )
    redirect_uri = (
        os.environ.get("X_BOOKMARKS_REDIRECT_URI")
        or config.get("redirect_uri")
        or DEFAULT_REDIRECT
    )

    allow_loopback_http(redirect_uri)

    if not client_id:
        raise RuntimeError(
            "No X client id found. Register an app at developer.x.com with "
            f"scopes {' '.join(SCOPES)} and redirect URI {redirect_uri}, then "
            f"write {{\"client_id\": \"...\"}} to {APP_PATH} "
            "(or set X_BOOKMARKS_CLIENT_ID)."
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }


###############################################################################
# Token persistence
###############################################################################


def write_token(token: dict[str, Any]) -> dict[str, Any]:
    """Persist the token at 0600, stamping an absolute expiry.

    The API hands back a relative `expires_in`, which is useless once it has
    been sitting on disk; `expires_at` is what later runs can actually check.

    Written through the store's atomic temp-file-and-rename path: a direct
    write_text creates the file world-readable until the later chmod and can
    truncate the only copy of the refresh token on a crash. mkstemp creates
    the temp file 0600, and rename carries that mode over.
    """
    from .store import write_json_atomic

    stamped = dict(token)
    if "expires_in" in stamped and "expires_at" not in stamped:
        try:
            stamped["expires_at"] = time.time() + float(stamped["expires_in"])
        except (TypeError, ValueError):
            pass

    write_json_atomic(TOKEN_PATH, stamped)
    return stamped


def read_token() -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("unreadable token file %s: %s", TOKEN_PATH, exc)
        return {}


def token_is_fresh(token: dict[str, Any]) -> bool:
    if not token.get("access_token"):
        return False
    expires_at = token.get("expires_at")
    if not expires_at:
        # No expiry recorded: assume stale and refresh rather than risk a
        # mid-run 401 that would look like a permissions problem.
        return False
    return float(expires_at) - EXPIRY_MARGIN_SECONDS > time.time()


###############################################################################
# Consent and refresh
###############################################################################


def build_authorization_url(app: dict[str, str]) -> tuple[str, Any]:
    """Authorization URL plus the handler holding this flow's PKCE verifier.

    The handler must survive until fetch_token: PKCE ties the code exchange to
    the verifier generated here, so a fresh handler cannot complete the flow.
    """
    import tweepy

    handler = tweepy.OAuth2UserHandler(
        client_id=app["client_id"],
        redirect_uri=app["redirect_uri"],
        scope=SCOPES,
        client_secret=app["client_secret"] or None,
    )
    return handler.get_authorization_url(), handler


def complete_consent(handler: Any, redirect_response: str) -> dict[str, Any]:
    """Exchange the pasted redirect URL for a token, and persist it."""
    token = handler.fetch_token(redirect_response.strip())
    saved = write_token(token)
    log.info("consent complete, token stored at %s", TOKEN_PATH)
    return saved


def refresh_token(app: dict[str, str], token: dict[str, Any]) -> dict[str, Any]:
    """Trade a refresh token for a new access token.

    Done with a direct POST rather than through tweepy: the refresh endpoint
    is a plain documented form post, and going direct keeps this working
    across tweepy versions whose session-level refresh support has moved.
    """
    import requests

    refresh = token.get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "Stored token has no refresh token. Re-run consent "
            "(ensure the offline.access scope is granted)."
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": app["client_id"],
    }
    auth = (
        (app["client_id"], app["client_secret"])
        if app.get("client_secret")
        else None
    )
    response = requests.post(TOKEN_URL, data=data, auth=auth, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"Token refresh failed ({response.status_code}): {response.text[:300]}"
        )

    fresh = response.json()
    # X does not always re-issue the refresh token; keep the old one if not.
    fresh.setdefault("refresh_token", refresh)
    return write_token(fresh)


def get_access_token() -> str:
    """A usable access token, refreshing if needed.

    Raises with an actionable message when consent has never been run, rather
    than returning empty and failing later as an opaque 401.
    """
    app = load_app_config()
    token = read_token()

    if not token:
        raise RuntimeError(
            "No stored token. Run the consent flow first: "
            "python cli.py auth"
        )
    if token_is_fresh(token):
        return str(token["access_token"])

    log.info("access token stale, refreshing")
    return str(refresh_token(app, token)["access_token"])
