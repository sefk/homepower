"""manage.py milestone: the list Grafana draws as chart markers."""

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import Milestone


def run(*args):
    out = StringIO()
    call_command("milestone", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestMilestoneCommand:
    def test_seeded_by_migration(self):
        labels = list(Milestone.objects.values_list("label", flat=True))
        assert labels == ["Electric dryer installed", "Sef retired", "Heat pump hot water heater"]

    def test_add_list_remove(self):
        run("add", "2027-03-01", "Battery installed")
        m = Milestone.objects.get(label="Battery installed")
        assert m.date == date(2027, 3, 1)
        assert "Battery installed" in run("list")
        run("remove", str(m.id))
        assert not Milestone.objects.filter(label="Battery installed").exists()

    def test_bad_date_rejected(self):
        with pytest.raises(CommandError):
            run("add", "03/01/2027", "Battery installed")

    def test_remove_unknown_id(self):
        with pytest.raises(CommandError):
            run("remove", "9999")
