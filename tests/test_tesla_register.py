"""manage.py tesla_register: refuses until the hosted key matches, then
registers the domain with a client-credentials token."""

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from collectors.management.commands import tesla_register

PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n"


@pytest.fixture
def configured(settings, tmp_path):
    key = tmp_path / "key.pem"
    key.write_text(PUBLIC_KEY)
    settings.TESLA_PUBLIC_KEY_FILE = key
    settings.TESLA_CLIENT_ID = "cid"
    settings.TESLA_CLIENT_SECRET = "csecret"
    settings.TESLA_REGION = "na"


def fake_calls(monkeypatch, responses):
    seen = []

    def call(req):
        seen.append(req)
        return responses.pop(0)

    monkeypatch.setattr(tesla_register, "_call", call)
    return seen


def test_unpublished_key_stops_before_any_tesla_call(configured, monkeypatch):
    seen = fake_calls(monkeypatch, [(404, "Not Found")])
    with pytest.raises(CommandError, match="publish it first"):
        call_command("tesla_register", "example.com")
    assert len(seen) == 1


def test_registers_domain_with_partner_token(configured, monkeypatch, capsys):
    seen = fake_calls(
        monkeypatch,
        [(200, PUBLIC_KEY), (200, json.dumps({"access_token": "ptok"})), (200, "{}")],
    )
    call_command("tesla_register", "example.com")
    register = seen[2]
    assert register.full_url == (
        "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/partner_accounts"
    )
    assert register.headers["Authorization"] == "Bearer ptok"
    assert json.loads(register.data) == {"domain": "example.com"}
    assert "registered example.com" in capsys.readouterr().out
