"""Which collectors run, based on what's configured in settings/.env.

A source with no credentials simply doesn't run — its absence shows up
honestly as unknown coverage, not as an error."""

import logging

from django.conf import settings

from .base import Collector
from .envoy import EnvoyCollector
from .solaredge import SolarEdgeCollector
from .solaredge_auth import SolarEdgeAuth
from .tesla import TeslaCollector

logger = logging.getLogger(__name__)


def enabled_collectors() -> list[Collector]:
    collectors: list[Collector] = []
    if settings.ENPHASE_USERNAME and settings.ENPHASE_PASSWORD:
        collectors.append(
            EnvoyCollector(
                host=settings.ENVOY_HOST,
                username=settings.ENPHASE_USERNAME,
                password=settings.ENPHASE_PASSWORD,
                token_file=settings.ENPHASE_TOKEN_FILE,
            )
        )
    else:
        logger.warning("envoy: no Enlighten credentials in .env, collector disabled")
    if (
        settings.SOLAREDGE_USERNAME
        and settings.SOLAREDGE_PASSWORD
        and settings.SOLAREDGE_SITE_ID
    ):
        collectors.append(
            SolarEdgeCollector(
                auth=SolarEdgeAuth(
                    username=settings.SOLAREDGE_USERNAME,
                    password=settings.SOLAREDGE_PASSWORD,
                    token_file=settings.SOLAREDGE_TOKEN_FILE,
                ),
                site_id=settings.SOLAREDGE_SITE_ID,
            )
        )
    else:
        logger.warning("solaredge: no portal login/site id in .env, collector disabled")
    if settings.TESLA_CLIENT_ID and settings.TESLA_REFRESH_TOKEN:
        collectors.append(
            TeslaCollector(
                client_id=settings.TESLA_CLIENT_ID,
                client_secret=settings.TESLA_CLIENT_SECRET,
                refresh_token=settings.TESLA_REFRESH_TOKEN,
                token_file=settings.TESLA_TOKEN_FILE,
                usage_file=settings.TESLA_USAGE_FILE,
                monthly_budget=settings.TESLA_MONTHLY_REQUEST_BUDGET,
                vin=settings.TESLA_VIN or None,
                region=settings.TESLA_REGION,
                home_lat=settings.HOME_LAT,
                home_lon=settings.HOME_LON,
            )
        )
    else:
        logger.warning("tesla: no client id/refresh token in .env, collector disabled")
    return collectors
