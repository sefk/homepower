"""One-time OAuth bootstrap: exchange an authorization code for a Tesla
Fleet API refresh token.

Manual paste flow only, per ops/tesla-setup.md -- no callback web view.
This process runs on the LAN with no public HTTPS endpoint, so the
redirect URI never actually receives Tesla's callback; the user copies
the `code` query parameter out of the browser's address bar by hand
after the (failed) redirect and pastes it here.
"""

import asyncio
import sys

import aiohttp
from django.conf import settings
from django.core.management.base import BaseCommand

TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
# Never actually reached (see module docstring) -- must exactly match
# one of the app's registered redirect URIs for the code exchange to
# succeed, even though nothing is listening on it.
REDIRECT_URI = "http://localhost:8425/auth/tesla/callback"
SCOPES = ["vehicle_device_data", "offline_access"]


def authorize_url(client_id: str) -> str:
    scope = "+".join(SCOPES)
    return (
        "https://auth.tesla.com/oauth2/v3/authorize"
        f"?client_id={client_id}&redirect_uri={REDIRECT_URI}"
        f"&response_type=code&scope={scope}&state=homepower"
    )


async def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"token exchange failed: HTTP {resp.status} {data}")
            return data


class Command(BaseCommand):
    help = "Exchange a Tesla OAuth authorization code for a refresh token (manual paste flow)"

    def handle(self, *args, **options):
        client_id = settings.TESLA_CLIENT_ID
        client_secret = settings.TESLA_CLIENT_SECRET
        if not client_id or not client_secret:
            self.stderr.write(
                "TESLA_CLIENT_ID / TESLA_CLIENT_SECRET are not set -- see ops/tesla-setup.md"
            )
            sys.exit(1)

        self.stdout.write("1. Open this URL, log in, and authorize:\n")
        self.stdout.write(f"   {authorize_url(client_id)}\n")
        self.stdout.write(
            "2. The browser will redirect to a localhost URL that fails to load --\n"
            "   that's expected. Copy the `code` query parameter out of its address bar.\n"
        )
        code = input("Paste the authorization code: ").strip()
        if not code:
            self.stderr.write("no code entered")
            sys.exit(1)

        try:
            data = asyncio.run(exchange_code(client_id, client_secret, code))
        except RuntimeError as exc:
            self.stderr.write(str(exc))
            sys.exit(1)

        refresh_token = data.get("refresh_token")
        if not refresh_token:
            self.stderr.write(f"no refresh_token in response: {data}")
            sys.exit(1)

        self.stdout.write("\nRefresh token:\n")
        self.stdout.write(f"  {refresh_token}\n")
        self.stdout.write("\nAdd this to .env:\n")
        self.stdout.write(f"  TESLA_REFRESH_TOKEN={refresh_token}\n")
