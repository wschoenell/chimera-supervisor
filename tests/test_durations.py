# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import datetime

import pytest

from chimera_supervisor.core.durations import format_duration, parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90s", 90),
        ("30m", 1800),
        ("2h", 7200),
        ("1h30m", 5400),
        ("-2h", -7200),
        ("-30m", -1800),
        ("+0.5h", 1800),
        ("1h 30m", 5400),
    ],
)
def test_parse_strings(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


def test_parse_bare_numbers_use_default_unit():
    assert parse_duration(2, default_unit="h").total_seconds() == 7200
    assert parse_duration(-0.5, default_unit="h").total_seconds() == -1800
    assert parse_duration(120, default_unit="s").total_seconds() == 120
    assert parse_duration("0.25", default_unit="h").total_seconds() == 900


def test_parse_timedelta_passthrough():
    delta = datetime.timedelta(minutes=5)
    assert parse_duration(delta) is delta


@pytest.mark.parametrize("bad", ["", "abc", "2x", "h", True, None, "1h2x"])
def test_parse_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize("text", ["90s", "30m", "2h", "1h30m", "-2h", "-30m"])
def test_format_round_trip(text):
    assert format_duration(parse_duration(text)) == text


def test_format_zero():
    assert format_duration(datetime.timedelta(0)) == "0s"
