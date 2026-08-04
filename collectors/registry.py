"""Which collectors run, based on what's configured in settings/.env.

A source with no credentials simply doesn't run — its absence shows up
honestly as unknown coverage, not as an error."""

import logging

from django.conf import settings

from .base import Collector
from .envoy import EnvoyCollector

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
    return collectors
