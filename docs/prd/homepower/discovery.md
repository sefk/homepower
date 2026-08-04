# Discovery — 635 Central Home Power Monitor

Tags: `[confirmed]` = read in code/data/on the wire, or user said it. `[assumed]` = gap-filling.

## Environment

- `[confirmed]` This work is happening *on* `studio`: Mac Studio, Apple M1 Max (`T6000`), Darwin 25.5.0, arm64, `10.10.0.200`.
- `[confirmed]` Docker Desktop 29.4.2 is installed and running. Existing containers: `postgres:17` up 4 days (belongs to another project, `datatalk`), plus stopped `grafana`, `prometheus`, `otel-collector`, and an OpenLibrary dev stack.
- `[confirmed]` `uv` and `python3` present at `/opt/homebrew/bin`.
- `[confirmed]` Repo `/Users/sefk/src/homepower` has **no commits**. Contents are the original one-page brief (since superseded by `prd.md`) and `docs/635-central-energy-analysis-gemini.md` only. Nothing has been built.
- `[confirmed]` A Synology NAS lives at `10.10.0.250`, and `sefklon-vm` / `buddy` (locally-administered MACs, so VMs) at `.251` / `.253`. There are always-on alternatives to studio on this LAN.
- `[assumed]` studio is a daily-driver desktop, not a headless server. Docker Desktop on macOS needs a logged-in GUI session, does not auto-start reliably after reboot, and the VM stops when macOS sleeps. Any 24/7 collector on this host inherits those gaps.

## Data sources — what is actually reachable today

### ADU solar — Enphase Envoy ✅ reachable
- `[confirmed]` Live at `10.10.0.222`, advertises `_enphase-envoy._tcp` over mDNS as `envoy`.
- `[confirmed]` `GET /info.xml` → serial `202251013070`, PN `800-00647-r10`, firmware **`D8.3.5167`**, `<web-tokens>true</web-tokens>`, `<imeter>true</imeter>`.
- `[confirmed]` **The Gemini doc is stale on this point.** It says "local network scraping of the Envoy web interface." Firmware D7+ with `web-tokens: true` requires a JWT minted from `entrez.enphaseenergy.com` using Enlighten cloud credentials before `/production.json` or `/ivp/*` will answer. Owner tokens expire (~1 year). **Token refresh is a standing operational requirement, not a one-time setup step.**
- `[confirmed]` `imeter: true` means metering hardware is present, so per-phase production (and possibly consumption) CTs are available, not just panel-level production.

### Main house solar — SolarEdge SE6000A-US ⚠️ bridge found, but it is a black box
- `[confirmed]` **Located the bridge: `10.10.0.135`, MAC `00:27:02:10:e4:ea`, OUI registered to SolarEdge Technologies.** Matches the user's description of "a little bridge box plugged into my home LAN."
- `[confirmed]` **It listens on nothing.** Swept ports 1–10240 on that host: zero open. No HTTP, no Modbus on 502 or 1502. Also swept all 254 LAN hosts for 502/1502 — nothing anywhere.
- `[confirmed]` So the bridge is a pure outbound cloud client. It uploads to SolarEdge and exposes no local interface at all.
- `[confirmed]` The inverter therefore has **no IP of its own** — it reaches the bridge over ZigBee or RS485, and the bridge does not proxy Modbus. Enabling "Modbus TCP" is not a setting anyone can flip here; there is no TCP listener to enable.
- `[assumed]` Three paths to main-house solar data, in ascending order of effort:
  1. **SolarEdge Monitoring cloud API** — ~300 requests/day, 15-minute granularity. Zero hardware. Coarser than the 1-minute target.
  2. **Modbus RTU over RS485**, wired to the inverter's terminal block, via a USB-RS485 adapter or a WiFi-RS485 bridge (~$15–40). Gives the full SunSpec register map at 1-second resolution, entirely locally. `[assumed]` Risk: if the SolarEdge bridge itself uses RS485 rather than ZigBee, the bus already has a master and cannot take a second one.
  3. **Put the inverter directly on ethernet**, retiring the bridge, then enable Modbus TCP on port 1502 from the inverter's own LCD menu.
- `[confirmed]` User's stated preference: would rather read the inverter directly than go through the cloud, "but that's not a big deal." → **v1 uses the cloud API; local Modbus is a documented, designed-for upgrade path, not a rewrite.**

### Grid — Rainforest Eagle 3 ⏳ not yet delivered
- `[confirmed]` User states it is purchased but has not arrived or been installed.
- `[assumed]` It pairs to the PG&E meter via the RIN and exposes a local REST/uploader API capable of near-real-time (seconds) whole-home import/export. It is the highest-value source in the system and the last to arrive — so the build order can't be "grid first."

### EV — Tesla Model S ✅ in scope
- `[assumed]` The Gemini doc says "Tesla Streaming API + TeslaMate." Tesla retired the Owner API; the current path is the **Fleet API**: register a developer application, host a public key at a domain you control, metered per-call pricing above a free tier.
- `[confirmed]` User already owns **`sef.kloninger.com`** — the public-key hosting requirement, normally the blocking step, is already satisfied. User considers the API-key work "fun," not scary. → **In scope, not deferred.**
- `[confirmed]` `tesla-fleet-api` 1.7.6 exists on PyPI, so this does not require pulling in TeslaMate (which would drag along its own Postgres and Grafana).

## Prior art / what the Gemini doc prescribes

- `[confirmed]` It specifies Home Assistant + TeslaMate + InfluxDB + PostgreSQL + Grafana — **five services and two databases**.
- `[confirmed]` The user's own PRD contradicts this: "I'm skeptical I need a full tsdb, maybe sqlite would be sufficient?" and "my stack of choice is python/django."
- `[assumed]` Volume check: 4 sources at 1-minute resolution ≈ 2.1M rows/year. At 1-second on the Eagle alone ≈ 31M rows/year. SQLite handles the former without noticing and the latter with a rollup strategy. **Nothing here justifies InfluxDB.** The stack in the Gemini doc is sized for a problem this house does not have.
- `[confirmed]` Home Assistant's built-in Energy Dashboard natively integrates all four sources and is free. `[assumed]` It gets ~80% of the stated goal with roughly a day of setup, which is the strongest argument against building anything.
- `[confirmed]` The stated design target is `agentsview.io`, described as "an EDA dashboard." The site returns **403 to automated fetches**, so what specifically is admired about it is unknown and must come from the user.

## Established facts worth designing against (from the Gemini bill analysis)

- `[confirmed]` TOU peak is 4–9pm daily, year-round. Annual true-up in April.
- `[confirmed]` 2025–26 true-up: net **+1,439 kWh**, **$458.26**. Summer exports, winter imports.
- `[confirmed]` Winter arbitrage delta is only **~5.4¢/kWh** peak-vs-off-peak — the doc itself concludes this kills the battery case and favors EV load-shifting.
- `[confirmed]` Gas: ~366 therms/year, concentrated Nov–Feb (91, 74, 94 therms) — the electrification target.

## Why this might be the wrong problem

**The framing to attack: "I need telemetry."** The Gemini analysis already answered the headline questions from twelve monthly bills. Battery arbitrage doesn't pay (5.4¢ delta). The annual deficit is 1,439 kWh / $458. Winter gas is 366 therms. If the actual goal is "should I add panels, a battery, or a heat pump," **that decision is already supportable without collecting a single new data point** — and a year of 1-minute telemetry would not change any of those three answers. A monitoring system would then be an elaborate way to re-derive a conclusion already in hand.

**The strongest counter, and probably the real justification:** billing data is a monthly scalar. It cannot tell you *which loads* create the winter 4–9pm peak — hot tub heater vs. dryer vs. EV charging vs. baseline. That decomposition is the only thing that makes the one lever the analysis actually endorses (shift load out of 4–9pm) actionable. If the PRD is scoped to *load attribution during peak windows*, it earns its keep. If it's scoped to "collect everything and see," it probably doesn't.

**A problem one level up:** the biggest number in the whole dataset is gas — ~366 therms concentrated in three winter months, and an electric monitoring system is structurally blind to it. Optimizing a $458 electric true-up while the furnace and water heater burn ~$840/yr of gas may be aiming at the smaller target. `[assumed]` PG&E gas data has no real-time local equivalent (the Eagle 3 reads the electric meter), so the thing most worth measuring is the thing hardest to measure.

**Status quo may be adequate.** Enphase's app, SolarEdge's portal, and the PG&E website each already show their own slice. The genuine gap is that no one view *nets* them — but Home Assistant's Energy Dashboard fills exactly that gap, for free, today.

**Who would object, and their best argument:** anyone who has maintained a self-hosted HA stack. Five containers and two databases on a *desktop Mac that sleeps and reboots* is a system that will be silently broken for weeks before anyone notices, and gapped data is worse than no data for exactly the year-over-year comparisons this is meant to support. Their argument is: run less software, on hardware that stays up, or don't bother.

## Decisions (user, 2026-08-03)

- `[confirmed]` **Building it is the point.** This is a project the user wants to own and hack on, not a minimal path to a decision. This retires the whole "why this might be the wrong problem" section as a *blocker* — the disconfirmation stands as analysis, but "you may not need this" is answered: the building is the goal. Design consequence: optimize for a codebase that is pleasant to extend, not for shortest time-to-answer.
- `[confirmed]` **HA collects, Django analyzes.** Home Assistant owns device integration (Envoy, Eagle 3, SolarEdge, and their auth/token churn); a Django + SQLite app owns storage, cost modeling, and the EDA dashboard. Split along the natural seam: HA is good at talking to hardware and bad at being a place to write analysis; Django is the reverse.
- `[confirmed]` **1-minute resolution.** ~2.1M rows/year across four sources. SQLite is comfortable; InfluxDB is formally off the table. Enough to see appliances switch on, which is what peak decomposition needs.
- `[confirmed]` **Runs on studio, gaps accepted.** No dedicated hardware. Design consequence, and it is load-bearing: **gaps are a normal condition, not an error state.** The schema, the ingest path, and every chart must tolerate missing intervals without silently interpolating them into fake data. Backfill from each source's cloud history (Enphase, SolarEdge, PG&E Green Button) is how gaps get healed after the fact — that makes backfill a first-class feature, not a nice-to-have.

## What "like agentsview" actually means (user, 2026-08-03)

Ranked as the user stated them:

1. `[confirmed]` **Reliability of collection and storage.** Named first. The system's primary virtue is that it does not lose data. This makes ingest durability and gap-healing the core of the product, not infrastructure underneath it.
2. `[confirmed]` **A rich built-in library of visuals and analyses — "so I don't have to think of what questions to ask."** This is the sharpest requirement in the whole project and it inverts the usual dashboard brief. The deliverable is not a blank canvas with good filters; it is an *opinionated set of pre-built analyses* that embody what's already known to be interesting about home energy. Design consequence: the PRD needs a concrete, enumerated catalog of analyses, and that catalog is the actual product.
3. `[confirmed]` **Clean, modern UI.**
4. `[confirmed]` **Lightweight, lights-out operation** — "just launch at startup, backgrounded and listening on a port." Docker acceptable for isolation but explicitly *not* required, and agentsview being even simpler than Docker is cited approvingly.

### The tension this creates with the earlier "HA collects, Django analyzes" decision

- `[confirmed]` Requirement 4 and the Home Assistant decision pull in opposite directions. HA Container is a ~1.5GB second system with its own onboarding, its own UI, its own recorder database, and its own upgrade treadmill. That is close to the opposite of "backgrounded and listening on a port."
- `[confirmed]` **Docker Desktop on macOS does not start until a user logs into the GUI.** studio reboots for upgrades; if it comes back to a login screen, every Docker-hosted collector stays down until someone logs in. A native `launchd` daemon starts at boot with no login. For a "lights out" requirement this is decisive, and it argues against Docker for the collector specifically.
- `[confirmed]` The strongest argument for HA was that it maintains the fiddly device integrations — especially the Envoy JWT dance. But that capability is available standalone: **`pyenphase` 3.1.0** (the same library HA's Enphase integration uses, including token refresh), **`solaredge-modbus` 0.8.0** for the eventual local-Modbus upgrade, and **`tesla-fleet-api` 1.7.6**. The Eagle 3 pushes plain XML/JSON to an HTTP endpoint you specify, which is a receiving view, not an integration.
- `[assumed]` So the HA dependency buys less than it first appeared, and costs exactly the property the user values most. Worth re-deciding explicitly rather than inheriting.

## Host reality, corrected

- `[confirmed]` studio is **on all the time, never sleeps, reboots only for upgrades.** The earlier `[assumed]` "desktop that sleeps" was wrong. Uptime is good.
- `[confirmed]` User: "I generally keep things running at home OK, but make mistakes from time to time." → Gaps come from operator error and upgrades, not from sleep.
- `[confirmed]` User explicitly notes gap-healing is **not** studio-specific — it is inherent to any self-hosted setup — and asked for it in the design regardless. Backfill/reconciliation is confirmed in scope on its own merits.

## Architecture resolved (user, 2026-08-03)

- `[confirmed]` **Home Assistant is dropped.** Collection is native Python under `launchd`, in one Django project. Pull sources use `pyenphase` / SolarEdge cloud API / `tesla-fleet-api`; the Eagle 3 pushes to a Django view. Starts at boot with no GUI login.
- `[confirmed]` Accepted cost: when a vendor changes its auth scheme, that maintenance is the user's. Judged acceptable because building and owning it is the stated goal.
- `[confirmed]` This supersedes the earlier "HA collects, Django analyzes" answer, which was given before the "lights out" requirement and the Docker-Desktop-GUI-login problem were on the table.

No open architectural questions remain. Proceeding to the PRD.
