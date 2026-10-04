# Tesla Fleet API setup

One-time bootstrap for the EV collector (`collectors/tesla.py`). The
Fleet API requires a registered developer app, a public key hosted over
HTTPS, and a partner-account registration call before any vehicle data
request will succeed.

## Cost

The Fleet API bills every request below HTTP 500 — including a sleeping
car's 408 — against a **$10 monthly credit** ([billing and limits][billing]).
The collector is shaped to stay inside it:

| State | What it does | Cadence |
| --- | --- | --- |
| Idle | `GET /vehicles/{vin}` state check; never wakes the car. Asleep ⇒ known 0 W (a charging car doesn't sleep). | 30 min |
| Online | one `vehicle_data` read (`charge_state` only) | on the idle check |
| Charging | `vehicle_data` only, until the car stops charging | 5 min |

That's roughly 100 requests/day. As a backstop against a polling bug,
every Fleet API call counts against `TESLA_MONTHLY_REQUEST_BUDGET`
(default 4000), tracked in `var/tesla_usage.json`; once spent, polls fail
without calling Tesla until the next month. Leaving billing details off
the developer app keeps Tesla from charging beyond the credit.

Power readings are snapshots, so a session's start can show up to 30
minutes late. Energy is exact regardless: each read also records
`ev_charge_energy_wh`, the change in the car's `charge_energy_added`
counter since the previous read.

## 1. Register a developer app

At [developer.tesla.com][dev], Create Fleet API Application:

- **Client Details**: grant type *Authorization Code and
  Machine-to-Machine*.
  - Allowed Origin URL: `https://sef.kloninger.com` (the domain that will
    host the public key; bare origin, no path).
  - Allowed Redirect URI: `http://localhost:8425/auth/tesla/callback` —
    must match `manage.py tesla_auth` exactly.
  - Allowed Returned URL: blank.
- **API & Scopes**: *Vehicle Information* only (`vehicle_device_data`).
  Add *Vehicle Location* only if you set `HOME_LAT`/`HOME_LON` for the
  geofence.
- **Billing Details**: skip.

Put the **Client ID** and **Client Secret** in `.env` as
`TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET`.

## 2. Generate and host the public key

```sh
mkdir -p var/tesla
openssl ecparam -name prime256v1 -genkey -noout -out var/tesla/tesla_private_key.pem
openssl ec -in var/tesla/tesla_private_key.pem -pubout -out var/tesla/com.tesla.3p.public-key.pem
```

`var/` is gitignored. Keep the private key; it's only needed again if
vehicle commands are ever added (this project is read-only).

Publish the public key at exactly
`https://sef.kloninger.com/.well-known/appspecific/com.tesla.3p.public-key.pem`.
For the Nikola site in `sefk.github.io`:

```sh
mkdir -p files/.well-known/appspecific
cp ~/src/homepower/var/tesla/com.tesla.3p.public-key.pem files/.well-known/appspecific/
nikola build && ls output/.well-known/appspecific/
nikola github_deploy
```

## 3. Register the partner account

```sh
uv run python manage.py tesla_register sef.kloninger.com
```

Checks the hosted key matches `var/tesla/`, gets a client-credentials
token, and registers the domain in `TESLA_REGION` (`na` by default).

## 4. Get a refresh token

```sh
uv run python manage.py tesla_auth
```

Open the printed URL, log in with the Tesla account that owns the car,
and approve. Tesla redirects to
`http://localhost:8425/auth/tesla/callback?code=...` — if homepower is
running that page is a 404, which is expected. Copy the `code` value
from the address bar (it's single-use and expires within minutes) and
paste it into the prompt. Put the printed token in `.env` as
`TESLA_REFRESH_TOKEN`.

## 5. `.env`

```sh
TESLA_CLIENT_ID=...
TESLA_CLIENT_SECRET=...
TESLA_REFRESH_TOKEN=...     # printed by tesla_auth
#TESLA_VIN=                 # optional; blank uses the account's first vehicle (one billed call at startup)
#TESLA_REGION=na            # na | eu | cn
#TESLA_MONTHLY_REQUEST_BUDGET=4000
#HOME_LAT=
#HOME_LON=                  # geofence; needs the Vehicle Location scope. Superchargers are excluded regardless.
```

Restart the service and check `/health/` — the `tesla` source shows
live coverage once the car has been polled. The refresh token rotates
on every use; the collector persists the current one to
`var/tesla_token.json`, so `tesla_auth` only needs re-running if that
file is lost or the token is revoked.

[billing]: https://developer.tesla.com/docs/fleet-api/billing-and-limits
[dev]: https://developer.tesla.com
