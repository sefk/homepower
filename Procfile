# Run the whole stack in the foreground:  honcho start
# Stop the launchd daemons first (see ops/README.md), or the two sets of
# collectors will both poll the vendors. To try it alongside them anyway:
#   HOMEPOWER_PORT=8426 GRAFANA_PORT=3426 honcho start grafana
app: uv run python manage.py serve --port ${HOMEPOWER_PORT:-8425}
grafana: GF_SERVER_HTTP_PORT=${GRAFANA_PORT:-3425} grafana server --config ops/grafana/grafana.ini --homepath /opt/homebrew/opt/grafana/share/grafana --packaging=brew
