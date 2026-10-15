"""Back up the SQLite database to the external drive and prune old copies.

    manage.py backup_db                      # to /Volumes/ext1/homepower_backups
    manage.py backup_db --dest /some/dir
    manage.py backup_db --no-archive         # skip the Synology copy

Then the same file is copied to the Synology's Archive share (homepower/),
which the NAS mirrors offsite. Unencrypted: nothing confidential in here.
The share is mounted for the copy and unmounted after, unless something
else (Finder, claude-backup) already has it mounted, in which case that
mount is used and left alone. If the NAS is
unreachable the local backup still stands and the command exits non-zero.

Uses SQLite's online backup, so it's consistent while the collectors keep
writing (copying db.sqlite3 alone would miss what's still in the -wal).
Keeps every backup from the last week, then the first backup of each ISO
week for a year. Run daily by ops/com.sefk.homepower-backup.plist.
"""

import contextlib
import gzip
import shutil
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

DEFAULT_DEST = Path("/Volumes/ext1/homepower_backups")
DEFAULT_SHARE = "//GUEST@synology.local/Archive"
ARCHIVE_SUBDIR = "homepower"
MOUNT_POINT = Path.home() / ".local/state/homepower-backup/mnt"
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


def prune(dest: Path, today: date) -> list[str]:
    """Delete what to_prune says from dest; returns the names removed."""
    days = [d for p in dest.iterdir() if (d := backup_day(p.name))]
    removed = []
    for d in to_prune(days, today):
        (dest / backup_name(d)).unlink()
        removed.append(backup_name(d))
    return removed


def mirror(src: Path, dest: Path) -> None:
    """Copy src into dest atomically (a half-copied file never has the
    real name, so a dropped connection can't pass for a backup)."""
    dest.mkdir(parents=True, exist_ok=True)
    partial = dest / f".{src.name}.partial"
    try:
        shutil.copyfile(src, partial)
        if partial.stat().st_size != src.stat().st_size:
            raise CommandError(f"short copy to {dest}")
        partial.replace(dest / src.name)
    finally:
        partial.unlink(missing_ok=True)


def existing_mount(share: str) -> Path | None:
    """Where this share is already mounted, if it is. macOS refuses a second
    mount of the same share ("File exists"), so a mount made by Finder or
    claude-backup has to be reused."""
    want = share.split("@")[-1].removeprefix("//").lower()
    out = subprocess.run(
        ["mount", "-t", "smbfs"], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        spec, _, rest = line.partition(" on ")
        if spec.split("@")[-1].removeprefix("//").lower() == want:
            return Path(rest.rsplit(" (", 1)[0])
    return None


@contextlib.contextmanager
def mounted(share: str, mount_point: Path = MOUNT_POINT):
    if (found := existing_mount(share)) is not None:
        yield found
        return
    mount_point.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mount_smbfs", "-N", share, str(mount_point)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise CommandError(f"could not mount {share}: {result.stderr.strip()}")
    try:
        yield mount_point
    finally:
        subprocess.run(["umount", str(mount_point)], capture_output=True)


class Command(BaseCommand):
    help = "Back up db.sqlite3 (daily for a week, weekly for a year)"

    def add_arguments(self, parser):
        parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
        parser.add_argument("--archive-share", default=DEFAULT_SHARE)
        parser.add_argument(
            "--no-archive", action="store_true", help="skip the Synology copy"
        )

    def handle(self, *args, dest, archive_share, no_archive, **options):
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

        for name in prune(dest, today):
            self.stdout.write(f"pruned {name}")

        if no_archive:
            return
        try:
            with mounted(archive_share) as root:
                archive = root / ARCHIVE_SUBDIR
                mirror(final, archive)
                self.stdout.write(f"copied to {archive_share}/{ARCHIVE_SUBDIR}")
                for name in prune(archive, today):
                    self.stdout.write(f"pruned {name} from archive")
        except (OSError, subprocess.SubprocessError) as e:
            raise CommandError(f"archive copy failed: {e}") from e
