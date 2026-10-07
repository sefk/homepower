"""backup_db: a restorable copy on the drive, and the retention schedule."""

import gzip
import sqlite3
from datetime import date, timedelta

import pytest
from django.core.management import CommandError, call_command

from core.management.commands.backup_db import backup_day, backup_name, to_prune
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
        call_command("backup_db", dest=tmp_path)
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
        call_command("backup_db", dest=tmp_path)
        assert not old.exists()
        assert other.exists()

    def test_refuses_missing_destination(self, tmp_path):
        with pytest.raises(CommandError, match="not mounted"):
            call_command("backup_db", dest=tmp_path / "nope")
