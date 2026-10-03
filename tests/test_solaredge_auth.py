"""SolarEdge portal login: PKCE password flow, refresh, token caching."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from collectors.solaredge_auth import (
    CALLBACK_URL,
    LOGIN_BASE,
    TOKEN_URL,
    SolarEdgeAuth,
    SolarEdgeAuthError,
)

# The hosted login page: the credential form first, corporate SSO second.
# Only the first form's hidden fields may be echoed back.
LOGIN_HTML = """
<form method="post" action="/login?state=abc">
  <input type="hidden" name="csrf" value="csrf-token"/>
  <input name="username" type="email" value=""/>
  <input name="password" type="password" value=""/>
</form>
<form method="post" action="/login/oidc?state=abc">
  <input type="hidden" name="csrf" value="csrf-token"/>
  <input name="idpEmail" type="text" value=""/>
</form>
"""


class FakeResponse:
    def __init__(self, status, text="", payload=None, headers=None):
        self.status = status
        self._text = text
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._payload


class FakePortal:
    """A scripted login.solaredge.com: login page, credential POST that
    redirects through one hop to the callback, and the token endpoint."""

    def __init__(self, password_ok=True, refresh_ok=True):
        self.password_ok = password_ok
        self.refresh_ok = refresh_ok
        self.calls = []

    def get(self, url, headers=None, **kw):
        return self.request("GET", url, headers=headers, **kw)

    def post(self, url, data=None, headers=None, **kw):
        return self.request("POST", url, data=data, headers=headers, **kw)

    def request(self, method, url, data=None, headers=None, **kw):
        self.calls.append({"method": method, "url": url, "data": data})
        if url == TOKEN_URL:
            if data["grant_type"] == "refresh_token":
                if not self.refresh_ok:
                    return FakeResponse(400, payload={"error": "invalid_grant"})
                # Cognito's refresh grant returns no new refresh token.
                return FakeResponse(200, payload={"access_token": "A2", "expires_in": 86400})
            return FakeResponse(
                200,
                payload={"access_token": "A1", "refresh_token": "R1", "expires_in": 86400},
            )
        if method == "GET" and url.startswith(f"{LOGIN_BASE}/login?"):
            return FakeResponse(200, text=LOGIN_HTML)
        if method == "POST":
            if not self.password_ok:
                return FakeResponse(400, text="<p>Incorrect username or password.</p>")
            return FakeResponse(302, headers={"Location": "/hop"})
        if url == f"{LOGIN_BASE}/hop":
            return FakeResponse(302, headers={"Location": f"{CALLBACK_URL}?code=the-code"})
        raise AssertionError(f"unexpected request {method} {url}")


def make_auth(tmp_path):
    return SolarEdgeAuth("me@example.com", "pw", tmp_path / "solaredge_token.json")


def test_password_login_posts_first_form_and_exchanges_code(tmp_path):
    portal = FakePortal()
    auth = make_auth(tmp_path)
    assert asyncio.run(auth.access_token(portal)) == "A1"

    post = next(c for c in portal.calls if c["method"] == "POST" and c["url"] != TOKEN_URL)
    assert post["url"] == f"{LOGIN_BASE}/login?state=abc"
    assert post["data"] == {"csrf": "csrf-token", "username": "me@example.com", "password": "pw"}

    token_call = portal.calls[-1]
    assert token_call["data"]["code"] == "the-code"
    # PKCE: the verifier sent at exchange matches the challenge sent at login.
    import base64
    import hashlib

    challenge = parse_qs(urlparse(portal.calls[0]["url"]).query)["code_challenge"][0]
    digest = hashlib.sha256(token_call["data"]["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge
    # The callback itself is never fetched.
    assert not any(c["url"].startswith(CALLBACK_URL) for c in portal.calls)


def test_tokens_are_saved_private_and_reused(tmp_path):
    portal = FakePortal()
    auth = make_auth(tmp_path)
    asyncio.run(auth.access_token(portal))
    saved = json.loads(auth.token_file.read_text())
    assert saved["refresh_token"] == "R1"
    assert auth.token_file.stat().st_mode & 0o777 == 0o600

    # A fresh process picks the cached token up without any network call.
    again = FakePortal()
    assert asyncio.run(make_auth(tmp_path).access_token(again)) == "A1"
    assert again.calls == []


def test_expired_token_uses_refresh_and_keeps_refresh_token(tmp_path):
    auth = make_auth(tmp_path)
    auth.token_file.write_text(
        json.dumps({"access_token": "old", "refresh_token": "R1", "expires_at": time.time() - 1})
    )
    portal = FakePortal()
    assert asyncio.run(auth.access_token(portal)) == "A2"
    assert [c["url"] for c in portal.calls] == [TOKEN_URL]
    assert json.loads(auth.token_file.read_text())["refresh_token"] == "R1"


def test_force_renews_an_unexpired_token(tmp_path):
    auth = make_auth(tmp_path)
    auth.token_file.write_text(
        json.dumps({"access_token": "A1", "refresh_token": "R1", "expires_at": time.time() + 9999})
    )
    assert asyncio.run(auth.access_token(FakePortal(), force=True)) == "A2"


def test_rejected_refresh_falls_back_to_password(tmp_path):
    auth = make_auth(tmp_path)
    auth.token_file.write_text(
        json.dumps({"access_token": "old", "refresh_token": "dead", "expires_at": 0})
    )
    assert asyncio.run(auth.access_token(FakePortal(refresh_ok=False))) == "A1"


def test_wrong_password_raises_with_portal_reason(tmp_path):
    auth = make_auth(tmp_path)
    with pytest.raises(SolarEdgeAuthError, match="Incorrect username or password"):
        asyncio.run(auth.access_token(FakePortal(password_ok=False)))
    assert not auth.token_file.exists()
