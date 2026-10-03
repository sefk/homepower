"""Exercise every dashboard query against a running Grafana.

Grafana renders a broken panel as an empty chart, which looks a lot like a
genuine data gap — exactly the confusion this project exists to avoid. This
runs every target in ops/grafana/dashboards/ through the datasource API and
reports the SQL errors instead.

Template variables and $__from / $__to are normally interpolated in the
browser, so they are substituted here the same way before posting.

    python3 ops/grafana/check_panels.py            # exit 1 if any query fails
"""

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:3425"
DASHBOARDS = pathlib.Path(__file__).parent / "dashboards"

NOW_MS = int(time.time() * 1000)
FROM_MS = NOW_MS - 6 * 3600 * 1000

# Stand-ins for what the dashboard's variable pickers would hold.
VARS = {
    "$solar": "envoy/production_w",
    "$grid": "eagle/demand_w",
    "$series": "eagle/demand_w",
    "$__from": str(FROM_MS),
    "$__to": str(NOW_MS),
    "$__interval_ms": "900000",
}


def query(target):
    payload = {"queries": [target], "from": str(FROM_MS), "to": str(NOW_MS)}
    req = urllib.request.Request(
        BASE + "/api/ds/query",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["results"][target["refId"]]


def interpolate(sql):
    for name, value in VARS.items():
        sql = sql.replace(name, value)
    return sql


def check(label, datasource, sql, query_type="table", time_columns=()):
    sql = interpolate(sql)
    result = query(
        {
            "refId": "A",
            "datasource": datasource,
            "queryType": query_type,
            "timeColumns": list(time_columns),
            "rawQueryText": sql,
            "queryText": sql,
        }
    )
    if result.get("error"):
        print(f"  {label}: ERROR {result['error']}")
        return False
    frames = result.get("frames", [])
    rows = len(frames[0]["data"]["values"][0]) if frames and frames[0]["data"]["values"] else 0
    cols = [f["name"] for frame in frames for f in frame["schema"]["fields"] if f["name"] != "time"]
    print(f"  {label}: {rows} rows, columns {cols}")
    return True


def main():
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=5)
    except urllib.error.URLError as exc:
        sys.exit(f"Grafana is not answering on {BASE}: {exc}")

    failures = 0
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text())
        print(f"\n=== {dashboard['title']}")
        for variable in dashboard.get("templating", {}).get("list", []):
            failures += not check(
                f"var {variable['name']}", variable["datasource"], variable["query"]
            )
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                failures += not check(
                    f"[{panel['id']}] {panel['title']} ({target['refId']})",
                    target["datasource"],
                    target["rawQueryText"],
                    target["queryType"],
                    target["timeColumns"],
                )

    print(f"\n{failures} failing queries")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
