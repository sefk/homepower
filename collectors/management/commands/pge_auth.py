"""One-time PG&E sign-in: take the texted/emailed code, save the
remembered-device cookie the gas collector signs in with.

Interactive — run it yourself (`! uv run python manage.py pge_auth` from
Claude Code). Rerun whenever /health/ shows the pge_gas collector asking
for a new code.
"""

import asyncio

import aiohttp
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from opower import MeterType, MfaChallenge, Opower

from collectors.pge import UTILITY, save_login_data


class Command(BaseCommand):
    help = "Sign in to PG&E once (with the texted/emailed code) for the gas collector"

    def handle(self, *args, **options):
        if not settings.PGE_USERNAME or not settings.PGE_PASSWORD:
            raise CommandError("set PGE_USERNAME and PGE_PASSWORD in .env first")
        asyncio.run(self._sign_in())

    def _ask(self, prompt: str) -> str:
        return input(prompt).strip()

    async def _sign_in(self):
        async with aiohttp.ClientSession() as session:
            opower = Opower(session, UTILITY, settings.PGE_USERNAME, settings.PGE_PASSWORD)
            try:
                await opower.async_login()
                self.stdout.write("PG&E signed in without asking for a code.")
                login_data = {}
            except MfaChallenge as challenge:
                handler = challenge.handler
                choices = await handler.async_get_mfa_options()
                if choices:
                    keys = list(choices)
                    for i, key in enumerate(keys, 1):
                        self.stdout.write(f"  {i}. {key}: {choices[key]}")
                    pick = self._ask("Send the code by [1]: ") or "1"
                    try:
                        option = keys[int(pick) - 1]
                    except (ValueError, IndexError):
                        raise CommandError(f"not one of the choices: {pick!r}")
                    await handler.async_select_mfa_option(option)
                code = self._ask("Code from PG&E: ")
                login_data = await handler.async_submit_mfa_code(code)

        # Prove the saved cookie works on its own, the way the collector
        # will use it, before declaring success.
        async with aiohttp.ClientSession() as session:
            opower = Opower(
                session, UTILITY, settings.PGE_USERNAME, settings.PGE_PASSWORD,
                login_data=login_data,
            )
            try:
                await opower.async_login()
            except MfaChallenge:
                raise CommandError("PG&E still wants a code after sign-in; nothing saved")
            accounts = await opower.async_get_accounts()

        save_login_data(settings.PGE_LOGIN_FILE, login_data)
        gas = [a for a in accounts if a.meter_type == MeterType.GAS]
        self.stdout.write(f"Saved {settings.PGE_LOGIN_FILE}.")
        if expiry := login_data.get("expiryDateTime"):
            self.stdout.write(f"PG&E says this sign-in lasts until {expiry}.")
        self.stdout.write(
            f"{len(gas)} gas account(s) found."
            + ("" if gas else " The gas collector will fail until one shows up.")
        )
