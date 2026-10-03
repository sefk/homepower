"""Enphase Enlighten cloud history — the backfill path for the Envoy.

The Envoy's local API only reports the present; history lives in the
Enlighten cloud. The public Enphase API needs a developer app, but the
Enlighten web app reads its own JSON endpoints with the same login the
Envoy collector already uses to mint its local token, so this follows
the web app. Unpublished API: expect it to change without notice.

History is quarter-hour energy (Wh), coarser than the Envoy's one-minute
live polling. It is stored as the mean power over each quarter on the
same production_w series.
"""

from datetime import date, datetime, timezone as dt_timezone

from .base import Reading

BASE = "https://enlighten.enphaseenergy.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)


class EnlightenError(RuntimeError):
    pass


class EnlightenHistory:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.system_id: int | None = None

    async def login(self, session) -> None:
        """Sign in; the session's cookie jar carries the login from here."""
        data = {"user[email]": self.username, "user[password]": self.password}
        async with session.post(
            f"{BASE}/login/login.json?", data=data, headers={"User-Agent": USER_AGENT}
        ) as resp:
            if resp.status != 200:
                raise EnlightenError(f"enlighten login HTTP {resp.status}")
            payload = await resp.json(content_type=None)
        if payload.get("message") != "success" or not payload.get("system_id"):
            raise EnlightenError("enlighten login rejected")
        self.system_id = payload["system_id"]

    async def first_day(self, session) -> date:
        """The first day Enlighten has any production for."""
        payload = await self._get(session, "lifetime_energy")
        return date.fromisoformat(payload["start_date"])

    async def fetch(self, session, start: date) -> tuple[list[Reading], int]:
        """Up to 30 days of quarter-hours from `start`.

        Returns the readings and how many days the vendor answered for,
        so the caller can advance by exactly that.
        """
        payload = await self._get(session, "daily_energy", start_date=start.isoformat())
        return parse_daily_energy(payload), len(payload.get("stats") or [])

    async def _get(self, session, endpoint: str, **params) -> dict:
        url = f"{BASE}/pv/systems/{self.system_id}/{endpoint}"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with session.get(
            url, params=params or None, headers=headers, allow_redirects=False
        ) as resp:
            # An expired login answers with a redirect to the sign-in page.
            if resp.status != 200:
                raise EnlightenError(f"enlighten {endpoint} HTTP {resp.status}")
            return await resp.json(content_type=None)


def parse_daily_energy(payload: dict) -> list[Reading]:
    # Intervals past the Envoy's last report are zero-filled placeholders,
    # not measurements: keeping them would record unreported production
    # as a known zero.
    last_report = payload.get("last_report_date") or 0
    readings = []
    for day in payload.get("stats") or []:
        length = day["interval_length"]
        for i, wh in enumerate(day.get("production") or []):
            # start_time is an epoch, so stepping by seconds stays correct
            # across both DST edges.
            start = day["start_time"] + i * length
            if wh is None or start + length > last_report:
                continue
            readings.append(
                Reading(
                    metric="production_w",
                    unit="W",
                    ts=datetime.fromtimestamp(start, tz=dt_timezone.utc),
                    duration_s=length,
                    value=wh * 3600.0 / length,
                )
            )
    return readings
