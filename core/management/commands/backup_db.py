"""Back up the SQLite database to the external drive and prune old copies.

    manage.py backup_db                      # to /Volumes/ext1/homepower_backups
    manage.py backup_db --dest /some/dir

Uses SQLite's online backup, so it's consistent while the collectors keep
writing (copying db.sqlite3 alone would miss what's still in the -wal).
Keeps every backup from the last week, then the first backup of each ISO
week for a year. Run daily by ops/com.sefk.homepower-backup.plist.
"""

import gzip
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

DEFAULT_DEST = Path("/Volumes/ext1/homepower_backups")
PREFIX = "homepower-"
SUFFIX = ".sqlite3.gz"
DAILY_DAYS = 7
WEEKLY_DAYS = 365


def backup_name(day: date) -> str:
    return f"{PREFIX}{day.isoformat()}{SUFFIX}"


def backup_day(name: str) -> date | None:
    if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
        return None
    try:
        return date.fromisoformat(name[len(PREFIX) : -len(SUFFIX)])
    except ValueError:
        return None


def to_prune(days: list[date], today: date) -> list[date]:
    """The backup days to delete: older than a week and not their ISO
    week's first, or older than a year."""
    first_of_week: dict[tuple[int, int], date] = {}
    for d in sorted(days):
        first_of_week.setdefault(d.isocalendar()[:2], d)
    doomed = []
    for d in days:
        age = (today - d).days
        if age < DAILY_DAYS:
            continue
        if age < WEEKLY_DAYS and first_of_week[d.isocalendar()[:2]] == d:
            continue
        doomed.append(d)
    return sorted(doomed)


class Command(BaseCommand):
    help = "Back up db.sqlite3 (daily for a week, weekly for a year)"

    def add_arguments(self, parser):
        parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)

    def handle(self, *args, dest, **options):
        # Writing under an unmounted /Volumes/ext1 would fill the boot disk.
        if not dest.is_dir():
            raise CommandError(f"backup directory missing (drive not mounted?): {dest}")

        today = timezone.localdate()
        final = dest / backup_name(today)
        raw = dest / f".{final.name}.partial.sqlite3"
        gz = dest / f".{final.name}.partial"
        try:
            connection.ensure_connection()
            out = sqlite3.connect(raw)
            try:
                connection.connection.backup(out)
                check = out.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                out.close()
            if check != "ok":
                raise CommandError(f"backup failed quick_check: {check}")
            with open(raw, "rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out, 1 << 20)
            gz.replace(final)
        finally:
            raw.unlink(missing_ok=True)
            gz.unlink(missing_ok=True)
        self.stdout.write(f"wrote {final} ({final.stat().st_size / 1e6:.1f} MB)")

        days = [d for p in dest.iterdir() if (d := backup_day(p.name))]
        for d in to_prune(days, today):
            (dest / backup_name(d)).unlink()
            self.stdout.write(f"pruned {backup_name(d)}")
