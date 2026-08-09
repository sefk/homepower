# Tesla Fleet API setup

One-time bootstrap for the EV collector (`collectors/tesla.py`). The
Fleet API requires a registered developer app, a public key hosted over
HTTPS, and a partner-account registration call before any vehicle data
request will succeed — all one-time, all before `manage.py tesla_auth`
does anything useful.

PRD assumption: the free tier covers **one vehicle** at this poll rate
(60s); see `docs/prd/homepower/prd.md` "Assumptions". If that turns out
wrong, poll less often or drop to charge-session-only polling (only
poll while `charging_state == "Charging"` from the last known reading).

## 1. Register a developer app

At [developer.tesla.com](https://developer.tesla.com):

1. Create an application (Fleet API, not the legacy Owner API).
2. Redirect URI: `http://localhost:8425/auth/tesla/callback`. It never
   actually receives a callback (see step 4) — it only has to match
   exactly what `manage.py tesla_auth` sends, or the code exchange is
   rejected.
3. Scopes: `vehicle_device_data` (read charge/drive state) and
   `offline_access` (issues a refresh token, not just a short-lived
   access token).
4. Note the **Client ID** and **Client Secret** — these become
   `TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET` in `.env`.

## 2. Generate and host the public key

Tesla verifies the app by fetching a public key from a domain you
control, at a fixed well-known path:

```sh
openssl ecparam -name prime256v1 -genkey -noout -out tesla_private_key.pem
openssl ec -in tesla_private_key.pem -pubout -out tesla_public_key.pem
```

Host `tesla_public_key.pem` at exactly:

```
https://sef.kloninger.com/.well-known/appspecific/com.tesla.3p.public-key.pem
```

Keep `tesla_private_key.pem` — it's only needed again if a vehicle
command signing flow gets added later (this project is read-only, so
v1 doesn't need it for anything but the registration call below).

## 3. Register the partner account

One authenticated call, from wherever the private key isn't needed —
this only proves you control the public-key domain:

```sh
# 1. partner token (client_credentials grant, no user login)
curl -s https://auth.tesla.com/oauth2/v3/token -d grant_type=client_credentials \
  -d client_id=$TESLA_CLIENT_ID -d client_secret=$TESLA_CLIENT_SECRET \
  -d scope=openid -d audience=https://fleet-api.prd.na.vn.cloud.tesla.com \
  | jq -r .access_token > /tmp/partner_token

# 2. register the app against that region's Fleet API
curl -s -X POST https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/partner_accounts \
  -H "Authorization: Bearer $(cat /tmp/partner_token)" \
  -H "Content-Type: application/json" \
  -d '{"domain": "sef.kloninger.com"}'
```

Use `eu`/`cn` servers instead of `na` if the account isn't US-region
(`TESLA_REGION` in `.env` should match).

## 4. Get a refresh token

```sh
uv run python manage.py tesla_auth
```

This prints an authorization URL — open it, log in with the Tesla
account that owns the car, and approve. Tesla redirects to
`http://localhost:8425/auth/tesla/callback?code=...&state=...` — nothing
is listening there, so the page fails to load. That's expected: copy
the `code` value out of the browser's address bar and paste it back
into the command. It exchanges the code for a refresh token and prints
it.

There is deliberately no web callback view for this — a manual copy-paste
is simpler than standing up a route that only ever runs once.

## 5. `.env`

```sh
TESLA_CLIENT_ID=...
TESLA_CLIENT_SECRET=...
TESLA_REFRESH_TOKEN=...     # printed by tesla_auth
#TESLA_VIN=                 # optional; blank uses the account's first vehicle
#TESLA_REGION=na            # na | eu | cn
#HOME_LAT=
#HOME_LON=                  # best-effort home-charging geofence; blank = always home
```

Restart the service (or `manage.py serve`) and check `/health/` — the
`tesla` source should show live coverage once the car has been polled.
The refresh token rotates on every poll; the collector persists the
current one to `var/tesla_token.json`, same as the Envoy's JWT cache, so
this file only needs to be run again if that file is lost or the token
is revoked.
