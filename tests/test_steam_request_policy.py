from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from steam.request_policy import MarketCooldown, parse_retry_after


@pytest.mark.parametrize("raw", [None, "", "bad", "nan", "inf", "-5", "1e999"])
def test_unknown_retry_after_uses_finite_default(raw):
    assert parse_retry_after(raw) == 60


def test_retry_after_delta_and_http_date():
    now = 1780000000.0
    date = format_datetime(datetime.fromtimestamp(now + 120, timezone.utc), usegmt=True)
    assert parse_retry_after("120") == 120
    assert parse_retry_after(date, now=now) == 120
    assert parse_retry_after(date, now=now + 121) == 1
    assert parse_retry_after("0") == 1


def test_cooldown_does_not_shorten_or_extend_when_checked():
    now = [1000.0]
    cooldown = MarketCooldown(lambda: now[0])
    assert cooldown.remaining() == 0
    assert cooldown.defer("120") == 120
    now[0] += 10
    assert cooldown.defer("5") == 110
    assert cooldown.remaining() == 110
    now[0] += 111
    assert cooldown.remaining() == 0


def test_platform_date_conversion_failure_keeps_default_cooldown(monkeypatch):
    from steam import request_policy

    def invalid_date(_value):
        raise OSError("unsupported platform date")

    monkeypatch.setattr(request_policy, "parsedate_to_datetime", invalid_date)
    assert parse_retry_after("Sun, 01 Jan 1601 00:00:00 GMT") == 60


def test_repeated_429_without_retry_after_backs_off_progressively():
    now = [1000.0]
    cooldown = MarketCooldown(lambda: now[0])
    for expected in (60, 120, 240, 480, 900, 900):
        assert cooldown.defer() == expected
        now[0] += expected + 1


def test_server_retry_after_is_respected_after_repeated_limits():
    now = [1000.0]
    cooldown = MarketCooldown(lambda: now[0])
    cooldown.defer()
    now[0] += 61
    assert cooldown.defer("600") == 600


def test_success_resets_backoff_without_shortening_active_cooldown():
    now = [1000.0]
    cooldown = MarketCooldown(lambda: now[0])
    cooldown.defer()
    now[0] += 61
    assert cooldown.defer() == 120
    cooldown.record_success()
    assert cooldown.remaining() == 120
    now[0] += 121
    assert cooldown.defer() == 60
