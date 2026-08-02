"""Auth tests -- loopback transport and token freshness.

The consent flow itself needs a browser and a live authorization code, so it
is not covered here. What is covered is the logic that decides whether a
redirect is trustworthy and whether a stored token can still be used.
"""

import time

import pytest

from x_bookmarks import auth


###############################################################################
# Loopback detection
###############################################################################


@pytest.mark.parametrize("uri", [
    "http://127.0.0.1:8765/callback",
    "http://127.0.0.1/callback",
    "http://localhost:8765/callback",
    "http://[::1]:8765/callback",
])
def test_loopback_http_redirects_are_recognized(uri: str) -> None:
    assert auth.is_loopback_redirect(uri) is True


@pytest.mark.parametrize("uri", [
    "http://example.com/callback",
    "http://192.168.1.50:8765/callback",
    "http://myapp.local/callback",
    "https://127.0.0.1:8765/callback",
    "",
])
def test_non_loopback_or_https_is_not_a_loopback_http_redirect(uri: str) -> None:
    assert auth.is_loopback_redirect(uri) is False


def test_loopback_redirect_enables_insecure_transport(monkeypatch) -> None:
    """oauthlib refuses http outright; RFC 8252 requires it on loopback."""
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    assert auth.allow_loopback_http("http://127.0.0.1:8765/callback") is True
    import os
    assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"


def test_remote_http_redirect_does_not_enable_insecure_transport(monkeypatch) -> None:
    """The escape hatch must stay scoped -- a real insecure redirect still fails."""
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    assert auth.allow_loopback_http("http://example.com/callback") is False
    import os
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_load_app_config_enables_transport_for_default_redirect(monkeypatch) -> None:
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    monkeypatch.setenv("X_BOOKMARKS_CLIENT_ID", "abc")
    monkeypatch.setattr(auth, "APP_PATH", auth.APP_PATH.parent / "does-not-exist.json")

    config = auth.load_app_config()

    import os
    assert config["redirect_uri"] == auth.DEFAULT_REDIRECT
    assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"


###############################################################################
# App config resolution
###############################################################################


def test_env_client_id_wins_over_file(monkeypatch, tmp_path) -> None:
    app_file = tmp_path / "app.json"
    app_file.write_text('{"client_id": "from-file"}', encoding="utf-8")
    monkeypatch.setattr(auth, "APP_PATH", app_file)
    monkeypatch.setenv("X_BOOKMARKS_CLIENT_ID", "from-env")

    assert auth.load_app_config()["client_id"] == "from-env"


def test_missing_client_id_raises_with_actionable_message(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "APP_PATH", tmp_path / "absent.json")
    monkeypatch.delenv("X_BOOKMARKS_CLIENT_ID", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        auth.load_app_config()

    message = str(excinfo.value)
    assert "developer.x.com" in message
    assert "bookmark.read" in message


###############################################################################
# Token freshness
###############################################################################


def test_token_with_future_expiry_is_fresh() -> None:
    assert auth.token_is_fresh({
        "access_token": "t", "expires_at": time.time() + 3600,
    }) is True


def test_expired_token_is_not_fresh() -> None:
    assert auth.token_is_fresh({
        "access_token": "t", "expires_at": time.time() - 1,
    }) is False


def test_token_inside_the_margin_is_not_fresh() -> None:
    """Refresh early so a long resolve run cannot lose the token mid-flight."""
    assert auth.token_is_fresh({
        "access_token": "t",
        "expires_at": time.time() + auth.EXPIRY_MARGIN_SECONDS - 10,
    }) is False


def test_token_without_expiry_is_treated_as_stale() -> None:
    assert auth.token_is_fresh({"access_token": "t"}) is False


def test_token_without_access_token_is_not_fresh() -> None:
    assert auth.token_is_fresh({"expires_at": time.time() + 3600}) is False


def test_empty_token_is_not_fresh() -> None:
    assert auth.token_is_fresh({}) is False


###############################################################################
# Token persistence
###############################################################################


def test_write_token_stamps_absolute_expiry(monkeypatch, tmp_path) -> None:
    """expires_in is relative and useless once it has sat on disk."""
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "token.json")

    before = time.time()
    saved = auth.write_token({"access_token": "t", "expires_in": 7200})

    assert saved["expires_at"] >= before + 7200


def test_write_token_is_owner_readable_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "token.json")

    auth.write_token({"access_token": "t"})

    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600


def test_read_token_returns_empty_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "absent.json")
    assert auth.read_token() == {}


def test_unreadable_token_file_is_not_fatal(monkeypatch, tmp_path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(auth, "TOKEN_PATH", path)
    assert auth.read_token() == {}
