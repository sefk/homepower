"""One-time Tesla Fleet API partner registration (ops/tesla-setup.md).

Vehicle endpoints refuse an app until its domain is registered in the
region, which Tesla verifies by fetching the app's public key from that
domain. This checks the published key matches the local one, then makes
the call. It prints status and Tesla's reply, never the secret.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"


def _call(req: urllib.request.Request) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class Command(BaseCommand):
    help = "Register this app's domain with the Tesla Fleet API (one time)"

    def add_arguments(self, parser):
        parser.add_argument("domain", help="domain hosting the public key, e.g. example.com")

    def handle(self, *args, domain, **options):
        if not (settings.TESLA_CLIENT_ID and settings.TESLA_CLIENT_SECRET):
            raise CommandError("TESLA_CLIENT_ID / TESLA_CLIENT_SECRET not set in .env")
        local = settings.TESLA_PUBLIC_KEY_FILE.read_text().strip()
        key_url = f"https://{domain}{KEY_PATH}"
        status, body = _call(urllib.request.Request(key_url, headers={"User-Agent": "homepower"}))
        if status != 200 or body.strip() != local:
            raise CommandError(
                f"{key_url}: HTTP {status}, "
                f"{'matches' if body.strip() == local else 'does not match'} "
                f"{settings.TESLA_PUBLIC_KEY_FILE}; publish it first"
            )
        self.stdout.write(f"public key at {key_url} matches")

        audience = f"https://fleet-api.prd.{settings.TESLA_REGION}.vn.cloud.tesla.com"
        form = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": settings.TESLA_CLIENT_ID,
                "client_secret": settings.TESLA_CLIENT_SECRET,
                "scope": "openid vehicle_device_data",
                "audience": audience,
            }
        ).encode()
        status, body = _call(urllib.request.Request(TOKEN_URL, data=form))
        if status != 200:
            raise CommandError(f"partner token: HTTP {status}: {body[:500]}")
        token = json.loads(body)["access_token"]

        req = urllib.request.Request(
            f"{audience}/api/1/partner_accounts",
            data=json.dumps({"domain": domain}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        status, body = _call(req)
        if status != 200:
            raise CommandError(f"register {domain}: HTTP {status}: {body[:500]}")
        self.stdout.write(f"registered {domain} in region {settings.TESLA_REGION}")
