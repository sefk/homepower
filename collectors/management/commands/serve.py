"""The one process (PRD): web UI and collectors in a single event loop.

`manage.py serve` runs uvicorn programmatically alongside one asyncio
task per enabled collector. launchd starts this at boot; there is no
separate worker, queue, or cron.
"""

import asyncio
import logging

import uvicorn
from django.conf import settings
from django.core.management.base import BaseCommand

from collectors.registry import enabled_collectors
from collectors.scheduler import run_all

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the web UI and all enabled collectors in one process"

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.SERVE_HOST)
        parser.add_argument("--port", type=int, default=settings.SERVE_PORT)
        parser.add_argument(
            "--no-collect", action="store_true",
            help="serve the UI only (development)",
        )

    def handle(self, *args, host, port, no_collect, **options):
        asyncio.run(self._main(host, port, collect=not no_collect))

    async def _main(self, host: str, port: int, collect: bool) -> None:
        config = uvicorn.Config(
            "homepower.asgi:application",
            host=host,
            port=port,
            log_level="info",
            # uvicorn must not install its own signal handlers cleanly when
            # sharing the loop; Server.serve() handles that fine as a task.
        )
        server = uvicorn.Server(config)

        tasks = [asyncio.create_task(server.serve(), name="web")]
        if collect:
            collectors = enabled_collectors()
            logger.info(
                "starting collectors: %s", [c.slug for c in collectors] or "none"
            )
            tasks.append(asyncio.create_task(run_all(collectors), name="collectors"))

        # If either side dies, bring the whole process down so launchd
        # restarts it — a half-alive process would collect silently or
        # serve stale pages, and both are worse than an honest restart.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
