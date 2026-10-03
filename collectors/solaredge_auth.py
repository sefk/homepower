"""SolarEdge ONE portal login — username/password instead of an API key.

SolarEdge only issues Monitoring API keys through the installer account
that registered the site, which the owner of an out-of-warranty system
may never get. The web portal has no such gate: it signs in through an
OAuth PKCE flow against the hosted login at login.solaredge.com and then
calls monitoring.solaredge.com/services/ with a Bearer token. This is
that flow, as worked out by github.com/AndrewTapp/solaredgeoptimizers.

Access tokens last 24h. Tokens are cached in var/ across restarts and
renewed with the refresh token; the password login only runs when there
is no usable refresh token, so it happens rarely.
"""

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

logger = logging.getLogger(__name__)

LOGIN_BASE = "https://login.solaredge.com"
MONITORING_BASE = "https://monitoring.solaredge.com"
# The portal's public OAuth client (PKCE, no secret).
CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
CALLBACK_URL = f"{MONITORING_BASE}/mfe/auth/callback"
TOKEN_URL = f"{LOGIN_BASE}/oauth2/token"
# The hosted login serves its form only to browser-looking clients.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
# Renew this long before expiry so a token can't lapse mid-request.
EXPIRY_MARGIN_S = 300
MAX_REDIRECTS = 10


class SolarEdgeAuthError(RuntimeError):
    pass


class _FirstForm(HTMLParser):
    """Action and inputs of the first <form> (the second is corporate SSO)."""

    def __init__(self):
        super().__init__()
        self.action = ""
        self.inputs: dict[str, str] = {}
        self._state = 0  # 0 before, 1 inside, 2 after

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and self._state == 0:
            self._state = 1
            self.action = a.get("action") or ""
        elif tag == "input" and self._state == 1 and a.get("name"):
            self.inputs[a["name"]] = a.get("value") or ""

    def handle_endtag(self, tag):
        if tag == "form" and self._state == 1:
            self._state = 2


class SolarEdgeAuth:
    def __init__(self, username: str, password: str, token_file: Path):
        self.username = username
        self.password = password
        self.token_file = Path(token_file)
        self._tokens: dict | None = None

    async def access_token(self, session, force: bool = False) -> str:
        """A valid Bearer token. `force` discards the cached access token
        (the API rejected it) but still tries the refresh token first."""
        if self._tokens is None:
            self._tokens = self._load()
        tokens = self._tokens
        if not force and tokens.get("access_token") and (
            tokens.get("expires_at", 0) - EXPIRY_MARGIN_S > time.time()
        ):
            return tokens["access_token"]

        fresh = None
        if tokens.get("refresh_token"):
            fresh = await self._refresh(session, tokens["refresh_token"])
            if fresh is None:
                logger.info("solaredge refresh token rejected, logging in with password")
        if fresh is None:
            fresh = await self._password_login(session)
        # Cognito does not rotate the refresh token on a refresh grant.
        fresh.setdefault("refresh_token", tokens.get("refresh_token"))
        self._tokens = {
            "access_token": fresh["access_token"],
            "refresh_token": fresh.get("refresh_token"),
            "expires_at": time.time() + int(fresh.get("expires_in", 3600)),
        }
        self._save(self._tokens)
        return self._tokens["access_token"]

    async def _refresh(self, session, refresh_token: str) -> dict | None:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
        try:
            return await self._token_request(session, data)
        except SolarEdgeAuthError:
            return None

    async def _password_login(self, session) -> dict:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        params = {
            "lang": "en",
            "response_type": "code",
            "client_id": CLIENT_ID,
            "scope": "email openid",
            "redirect_uri": CALLBACK_URL,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        login_url = f"{LOGIN_BASE}/login?{urlencode(params)}"
        headers = {"User-Agent": USER_AGENT}
        async with session.get(login_url, headers=headers) as resp:
            if resp.status != 200:
                raise SolarEdgeAuthError(f"solaredge login page HTTP {resp.status}")
            html = await resp.text()

        form = _FirstForm()
        form.feed(html)
        # Hidden fields (csrf) must be echoed back with the credentials.
        body = {**form.inputs, "username": self.username, "password": self.password}
        url = urljoin(login_url, form.action) if form.action else login_url
        headers = {"User-Agent": USER_AGENT, "Origin": LOGIN_BASE, "Referer": login_url}

        # Follow redirects by hand: the authorization code rides on the
        # redirect to the portal callback, which never needs fetching.
        method, data = "POST", body
        for _ in range(MAX_REDIRECTS):
            async with session.request(
                method, url, data=data, headers=headers, allow_redirects=False
            ) as resp:
                location = resp.headers.get("Location")
                if not location:
                    text = await resp.text()
                    reason = re.search(r"(?i)(incorrect|invalid input)[^<\"]{0,80}", text)
                    raise SolarEdgeAuthError(
                        f"solaredge login HTTP {resp.status}"
                        + (f": {reason.group(0).strip()}" if reason else "")
                    )
            url = urljoin(url, location)
            method, data = "GET", None
            if url.startswith(CALLBACK_URL):
                break
        else:
            raise SolarEdgeAuthError("solaredge login never reached the callback")

        code = (parse_qs(urlparse(url).query).get("code") or [None])[0]
        if not code:
            raise SolarEdgeAuthError("solaredge login callback carried no code")
        logger.info("solaredge password login succeeded")
        return await self._token_request(
            session,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLIENT_ID,
                "redirect_uri": CALLBACK_URL,
                "code_verifier": verifier,
            },
        )

    async def _token_request(self, session, data: dict) -> dict:
        headers = {
            "User-Agent": USER_AGENT,
            "Origin": MONITORING_BASE,
            "Referer": f"{MONITORING_BASE}/",
        }
        async with session.post(TOKEN_URL, data=data, headers=headers) as resp:
            if resp.status != 200:
                raise SolarEdgeAuthError(
                    f"solaredge token ({data['grant_type']}) HTTP {resp.status}"
                )
            tokens = await resp.json()
        if not tokens.get("access_token"):
            raise SolarEdgeAuthError("solaredge token response had no access_token")
        return tokens

    def _load(self) -> dict:
        try:
            return json.loads(self.token_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, tokens: dict) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tokens))
        self.token_file.chmod(0o600)
