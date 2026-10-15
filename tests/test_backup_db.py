"""backup_db: a restorable copy on the drive, and the retention schedule."""

import gzip
import sqlite3
from datetime import date, timedelta

import pytest
from django.core.management import CommandError, call_command

from core.management.commands.backup_db import (
    backup_day,
    backup_name,
    mirror,
    prune,
    to_prune,
)
from core.models import Milestone

TODAY = date(2026, 10, 7)  # a Wednesday


class TestToPrune:
    def test_keeps_the_last_week(self):
        days = [TODAY - timedelta(days=n) for n in range(7)]
        assert to_prune(days, TODAY) == []

    def test_keeps_first_of_each_week_for_a_year(self):
        days = [TODAY - timedelta(days=n) for n in range(400)]
        kept = sorted(set(days) - set(to_prune(days, TODAY)))
        older = [d for d in kept if (TODAY - d).days >= 7]
        assert all(d.isoweekday() == 1 for d in older[1:])  # Mondays...
        assert all((TODAY - d).days < 365 for d in kept)
        # ...one per week; the oldest week may be cut off by the year limit
        assert len({d.isocalendar()[:2] for d in older}) == len(older)
        assert 51 <= len(older) <= 53

    def test_missed_monday_keeps_the_weeks_next_backup(self):
        tue = date(2026, 9, 1)
        days = [tue, tue + timedelta(days=1)]
        assert to_prune(days, TODAY) == [tue + timedelta(days=1)]

    def test_stable_from_day_to_day(self):
        days = [TODAY - timedelta(days=n) for n in range(60)]
        kept = set(days) - set(to_prune(days, TODAY))
        tomorrow = TODAY + timedelta(days=1)
        kept_tomorrow = (kept | {tomorrow}) - set(to_prune(list(kept | {tomorrow}), tomorrow))
        # tomorrow's run deletes at most the one daily that aged out
        assert len(kept - kept_tomorrow) <= 1

    def test_name_round_trip(self):
        assert backup_day(backup_name(TODAY)) == TODAY
        assert backup_day("notes.txt") is None
        assert backup_day("homepower-bogus.sqlite3.gz") is None


@pytest.mark.django_db(transaction=True)
class TestCommand:
    def test_writes_a_restorable_backup(self, tmp_path):
        Milestone.objects.create(date=TODAY, label="backup test")
        call_command("backup_db", dest=tmp_path, no_archive=True)
        (backup,) = tmp_path.iterdir()
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(gzip.decompress(backup.read_bytes()))
        conn = sqlite3.connect(restored)
        labels = [r[0] for r in conn.execute("select label from core_milestone")]
        conn.close()
        assert "backup test" in labels

    def test_prunes_old_backups(self, tmp_path):
        old = tmp_path / backup_name(date(2020, 1, 6))
        old.write_bytes(b"")
        other = tmp_path / "keep-me.txt"
        other.write_bytes(b"")
        call_command("backup_db", dest=tmp_path, no_archive=True)
        assert not old.exists()
        assert other.exists()

    def test_refuses_missing_destination(self, tmp_path):
        with pytest.raises(CommandError, match="not mounted"):
            call_command("backup_db", dest=tmp_path / "nope", no_archive=True)


class TestArchive:
    def test_mirror_copies_and_leaves_no_partial(self, tmp_path):
        src = tmp_path / backup_name(TODAY)
        src.write_bytes(b"data")
        mirror(src, tmp_path / "share" / "homepower")
        copied = list((tmp_path / "share" / "homepower").iterdir())
        assert [p.name for p in copied] == [src.name]
        assert copied[0].read_bytes() == b"data"

    def test_prune_applies_the_same_schedule(self, tmp_path):
        old = tmp_path / backup_name(date(2020, 1, 6))
        old.write_bytes(b"")
        recent = tmp_path / backup_name(TODAY)
        recent.write_bytes(b"")
        assert prune(tmp_path, TODAY) == [old.name]
        assert recent.exists()


@pytest.mark.django_db(transaction=True)
def test_unreachable_share_fails_after_local_backup(tmp_path, monkeypatch):
    import subprocess

    from core.management.commands import backup_db

    monkeypatch.setattr(
        backup_db.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 64, "", "no route"),
    )
    with pytest.raises(CommandError, match="could not mount"):
        call_command("backup_db", dest=tmp_path)
    assert len(list(tmp_path.iterdir())) == 1  # local backup stands


def test_existing_mount_matches_share_case_insensitively(monkeypatch):
    import subprocess

    from core.management.commands import backup_db

    listing = (
        "//GUEST:@synology.local/Archive on /Users/x/mnt (smbfs, nodev)\n"
        "//GUEST:@Synology._smb._tcp.local./Backup on /Volumes/tm (smbfs)\n"
    )
    monkeypatch.setattr(
        backup_db.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, listing, ""),
    )
    assert str(backup_db.existing_mount("//GUEST@Synology.local/archive")) == "/Users/x/mnt"
    assert backup_db.existing_mount("//GUEST@synology.local/Other") is None
