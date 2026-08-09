"""Which collectors run, based on what's configured in settings/.env.

A source with no credentials simply doesn't run — its absence shows up
honestly as unknown coverage, not as an error."""

import logging

from django.conf import settings

from .base import Collector
from .envoy import EnvoyCollector
from .solaredge import SolarEdgeCollector

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
    if settings.SOLAREDGE_API_KEY and settings.SOLAREDGE_SITE_ID:
        collectors.append(
            SolarEdgeCollector(
                api_key=settings.SOLAREDGE_API_KEY,
                site_id=settings.SOLAREDGE_SITE_ID,
            )
        )
    else:
        logger.warning("solaredge: no API key in .env, collector disabled")
    return collectors
