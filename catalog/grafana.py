"""Deep links into Grafana that follow the address the viewer came in by.

The app and Grafana are separate servers on separate ports, so a link
can't be a fixed URL: from studio:8425 Grafana is studio:3425, but through
a Tailscale front door on :443 there is only one origin and Grafana sits
under /grafana. Which case this is falls out of the port the request
arrived on."""

from urllib.parse import urlencode

from django.conf import settings
from django.http.request import split_domain_port


def _base(request) -> str:
    domain, port = split_domain_port(request.get_host())
    if str(port or request.get_port()) == str(settings.SERVE_PORT):
        scheme = "https" if request.is_secure() else "http"
        return f"{scheme}://{domain}:{settings.GRAFANA_PORT}{settings.GRAFANA_SUBPATH}"
    return settings.GRAFANA_PROXY_PATH


def url(request, uid: str | None = None, **params) -> str:
    """Grafana's home, or dashboard `uid`, with template variables as
    keyword args (`loads="Tesla"` -> `var-loads=Tesla`)."""
    base = _base(request)
    if uid is None:
        return base + "/"
    query = urlencode({f"var-{k}": v for k, v in params.items()})
    return f"{base}/d/{uid}" + (f"?{query}" if query else "")


def context(request) -> dict:
    return {"grafana_home": url(request)}
