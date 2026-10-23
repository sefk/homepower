# Run the whole stack in the foreground:  honcho start
# Stop the launchd daemons first (see ops/README.md), or the two sets of
# collectors will both poll the vendors and the ports will clash.
# HOMEPOWER_PORT and GRAFANA_PORT (or .env) move the ports.
app: uv run python manage.py serve --port ${HOMEPOWER_PORT:-8425}
grafana: GF_SERVER_HTTP_PORT=${GRAFANA_PORT:-3425} grafana server --config ops/grafana/grafana.ini --homepath /opt/homebrew/opt/grafana/share/grafana --packaging=brew
