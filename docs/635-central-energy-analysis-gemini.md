# 635-central-energy-analysis-gemini.md

## 1. Project Overview & Site Profile
This document serves as a data summary and technical specification for building a local home energy monitoring and analysis stack. The goal is to track, consolidate, and analyze energy generation and consumption to model future home electrification and potential battery storage additions.

*   **Location:** 635 Central Ave, Menlo Park, CA 94025.
*   **Property Size:** 2700 sq ft main home, 650 sq ft ADU. 
*   **Occupancy:** 2 adults, 1 dog, working from home/retired.
*   **Electrical Service:** Single 200A main service panel.
*   **Heavy Electric Loads:** Tesla Model S, 400+ gallon hot tub, electric dryer, AC (rarely used).
*   **Heavy Gas Loads:** Furnace, Hot Water Heater.

## 2. Utility & Billing Baseline
The system must be built to track usage against the specific Time-of-Use parameters defined by the local utility.

*   **Utility Provider (Delivery):** PG&E (Pacific Gas and Electric).
*   **Utility Provider (Generation):** Peninsula Clean Energy (PCE) / WestLight Energy.
*   **Electric Rate Plan:** Time-of-Use (Peak Pricing 4 - 9 p.m. Every Day).
    *   *Peak Window:* 4:00 PM - 9:00 PM, 7 days a week.
    *   *Off-Peak Window:* All other hours.
*   **Gas Rate Plan:** G1 XB Residential Service.
*   **True-Up Cycle:** Annual True-Up occurs in April.
*   **Smart Meter Credentials:**
    *   Electric Meter Number: 1003834933.
    *   Gas Meter Number: 839538G.
    *   Rate Identification Numbers (RIN): `USCA-PGXX-0100-0000` (PG&E) and `USCA-XXPE-0440-0000` (PCE).

## 3. Hardware Ecosystem & Data Sources
The monitoring stack will aggregate data from four distinct hardware/software sources without relying on proprietary cloud lock-in where possible.

### A. Main House Solar Array
*   **Hardware:** 25x SunPower SPR-X21-345 PV Modules with a central SolarEdge SE6000A-US Inverter. *(Note: While the 2015 proposal lists 20 panels, later documentation confirms 25 were installed).*
*   **Data Extraction Method:** SolarEdge Remote Monitoring Web API (using generated API keys) or via Local Area Network using Modbus TCP (if inverter is hardwired via Ethernet).

### B. ADU Solar Array
*   **Hardware:** 5 PV panels with Enphase IQ7+ microinverters connected to an Enphase AC Combiner box (installed 2022).
*   **Data Extraction Method:** Local network scraping of the Enphase Envoy gateway web interface (accessible via local IP).

### C. Grid Usage (PG&E Smart Meter)
*   **Hardware:** Rainforest Eagle 3 (Zigbee Smart Energy Gateway).
*   **Data Extraction Method:** The Eagle 3 pairs wirelessly to the PG&E smart meter using the utility RIN (`USCA-PGXX-0100-0000`). It exposes a Local API. The monitoring system will poll this local API for real-time total household wattage (imports and exports).

### D. Electric Vehicle Charging
*   **Hardware:** Tesla Model S.
*   **Data Extraction Method:** Tesla Streaming API. Deploy an open-source self-hosted logger (e.g., **TeslaMate**). This runs in Docker, authenticates via API tokens, and automatically logs exact kWh added, charge times, and geofenced locations (to isolate "Home" charging).

## 4. Comprehensive Historical Data (For Database Seeding & Modeling)
The following tabular data represents the actual historical grid interactions. This should be used to establish baseline dashboards, back-test load shifting algorithms, and validate the accuracy of real-time data collection. 

### A. Monthly Electric Usage & NEM Charges (May 2025 - July 2026)
The following data maps the monthly Net Energy Metering (NEM) balances. Note that a negative usage means the home exported more solar power than it consumed from the grid during that time window.

| Bill Period End Date | Net Peak Usage (kWh) | Net Off-Peak Usage (kWh) | Net Usage (kWh) | NEM Charges Before Taxes |
| :--- | :--- | :--- | :--- | :--- |
| 05/13/2025 | 3 | -436 | -433 | -$120.23 |
| 06/15/2025 | -182 | -861 | -1043 | -$343.43 |
| 07/15/2025 | -136 | -588 | -724 | -$249.07 |
| 08/13/2025 | -58 | -357 | -415 | -$130.57 |
| 09/14/2025 | 51 | -232 | -181 | -$49.37 |
| 10/14/2025 | 147 | -184 | -37 | -$8.42 |
| 11/13/2025 | 323 | 207 | 530 | $148.42 |
| 12/15/2025 | 474 | 563 | 1037 | $316.27 |
| 01/14/2026 | 482 | 913 | 1395 | $464.64 |
| 02/16/2026 | 453 | 508 | 960 | $327.37 |
| 03/17/2026 | 209 | 7 | 216 | $72.43 |
| 04/15/2026 | 114 | 20 | 134 | $30.22 |
| **2025-2026 True-Up Totals** | **1,880** | **-440** | **1,439** | **$458.26** |
| 05/14/2026 | -9 | -171 | -180 | -$40.05 |
| 06/14/2026 | -39 | -447 | -486 | -$128.89 |
| 07/14/2026 | -10 | -161 | -171 | -$44.69 |

### B. Monthly Gas Usage (Oct 2025 - July 2026)
To accurately model the required solar additions for home electrification (heat pumps for the furnace and water heater), the system must account for the winter gas deficit. *(Note: Data for late-summer 2025 is not included in the provided bills).*

| Billing Period Dates | Gas Usage (Therms) | Total Gas Charges | Source Reference |
| :--- | :--- | :--- | :--- |
| 10/16/2025 - 11/14/2025 | 25.0 | $70.84 | |
| 11/15/2025 - 12/16/2025 | 91.0 | $279.86 | |
| 12/17/2025 - 01/15/2026 | 74.0 | $219.24 | |
| 01/16/2026 - 02/17/2026 | 94.0 | $267.33 | |
| 02/18/2026 - 03/18/2026 | 30.0 | $74.42 | |
| 03/19/2026 - 04/16/2026 | 17.0 | $41.43 | |
| 04/17/2026 - 05/15/2026 | 16.0 | $38.95 | |
| 05/16/2026 - 06/15/2026 | 9.0 | $22.11 | |
| 06/16/2026 - 07/15/2026 | 10.0 | $25.56 | |

### C. Rate Modeling (Cost Analysis & Arbitrage Limits)
The data pipeline should calculate estimated costs using these approximate variables based on the historical bills:
*   *Summer Rates (June - Sept):* Peak (~$0.66/kWh) | Off-Peak (~$0.45/kWh).
*   *Winter Rates (Oct - May):* Peak (~$0.625/kWh) | Off-Peak (~$0.571/kWh).
*   *Arbitrage Delta:* The mathematical maximum savings margin for battery load shifting in winter is only ~5.4 cents per kWh, prioritizing EV load-shifting over physical battery acquisition.

## 5. Software Stack Architecture (For Coding Agent)
To meet the requirement of complete local ownership and control, the following stack should be deployed:

1.  **Collector / Automation Hub:** `Home Assistant` (OS or Containerized).
    *   *Integrations to configure:* Rainforest Eagle (Local), SolarEdge (Modbus/API), Enphase Envoy (Local).
2.  **EV Data Logger:** `TeslaMate` (Docker container).
3.  **Time-Series Database:** `InfluxDB` (Target for Home Assistant long-term energy statistics) and `PostgreSQL` (Default backing for TeslaMate).
4.  **Visualization:** `Grafana` (Connected to both InfluxDB and Postgres to build unified dashboards combining Solar, Grid, and EV telemetry). 
```
