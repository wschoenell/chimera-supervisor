# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import datetime

import pytest

from chimera_supervisor.core.conditions import (
    TransientMemory,
    parse_condition,
)
from chimera_supervisor.core.exceptions import ConfigError
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag

from .fakes import make_context


def evaluate(cfg, ctx, memory=None):
    condition = parse_condition(cfg, "test")
    return condition.evaluate(ctx, memory or TransientMemory())


# ----------------------------------------------------------------------
# time
# ----------------------------------------------------------------------


def test_time_after_sunset_passes_at_night():
    ctx = make_context()
    passed, message = evaluate({"condition": "time", "after": "sunset"}, ctx)
    assert passed, message


def test_time_before_sunrise_with_offset():
    ctx = make_context()
    # sunrise at 10:00, now 03:00 -> before sunrise-2h (08:00) passes
    assert evaluate({"condition": "time", "before": "sunrise", "offset": "-2h"}, ctx).passed
    # before sunrise-8h (02:00) fails
    assert not evaluate({"condition": "time", "before": "sunrise", "offset": "-8h"}, ctx).passed


def test_time_fixed_hhmm():
    ctx = make_context()  # now is 03:00 UT
    assert evaluate({"condition": "time", "after": "02:30"}, ctx).passed
    assert not evaluate({"condition": "time", "after": "04:00"}, ctx).passed
    assert evaluate({"condition": "time", "before": "04:00"}, ctx).passed


def test_time_requires_exactly_one_direction():
    with pytest.raises(ConfigError):
        parse_condition({"condition": "time", "after": "sunset", "before": "sunrise"}, "test")
    with pytest.raises(ConfigError):
        parse_condition({"condition": "time"}, "test")


def test_time_rejects_unknown_reference():
    with pytest.raises(ConfigError):
        parse_condition({"condition": "time", "after": "noonish"}, "test")


# ----------------------------------------------------------------------
# dome / telescope / weather station
# ----------------------------------------------------------------------


def test_dome_slit_and_flap():
    ctx = make_context()
    assert evaluate({"condition": "dome", "slit": "closed"}, ctx).passed
    ctx.domes[0].slit_open = True
    assert evaluate({"condition": "dome", "slit": "open"}, ctx).passed
    assert not evaluate({"condition": "dome", "flap": "open"}, ctx).passed


def test_telescope_states():
    ctx = make_context()
    tel = ctx.telescopes[0]
    assert evaluate({"condition": "telescope", "state": "parked"}, ctx).passed
    tel.parked = False
    tel.tracking = True
    assert evaluate({"condition": "telescope", "state": "unparked"}, ctx).passed
    assert evaluate({"condition": "telescope", "state": "tracking"}, ctx).passed
    assert not evaluate({"condition": "telescope", "state": "not_tracking"}, ctx).passed


def test_telescope_m1_temperature():
    ctx = make_context()
    ctx.telescopes[0].sensors = [("TM1", 10.0), ("FrontRing", 12.0)]
    assert evaluate({"condition": "telescope", "state": "m1_cooler_than_front_ring"}, ctx).passed
    ctx.telescopes[0].sensors = [("TM1", 14.0), ("FrontRing", 12.0)]
    assert evaluate({"condition": "telescope", "state": "m1_warmer_than_front_ring"}, ctx).passed


def test_weather_station_health():
    ctx = make_context()
    assert evaluate({"condition": "weather_station", "station": 0, "state": "ok"}, ctx).passed
    ctx.weather_stations[0].stale = True
    assert evaluate({"condition": "weather_station", "station": 0, "state": "stale"}, ctx).passed
    # out-of-range station counts as stale
    assert evaluate({"condition": "weather_station", "station": 5, "state": "stale"}, ctx).passed


# ----------------------------------------------------------------------
# weather thresholds
# ----------------------------------------------------------------------


def test_humidity_above():
    ctx = make_context()
    ctx.weather_stations[0].values["humidity"] = 90.0
    assert evaluate({"condition": "humidity", "above": 85}, ctx).passed
    ctx.weather_stations[0].values["humidity"] = 60.0
    assert not evaluate({"condition": "humidity", "above": 85}, ctx).passed


def test_dew_gap_below():
    ctx = make_context()
    ctx.weather_stations[0].values.update(temperature=10.0, dew_point=8.0)
    assert evaluate({"condition": "dew_gap", "below": 4}, ctx).passed
    ctx.weather_stations[0].values.update(temperature=15.0, dew_point=5.0)
    assert not evaluate({"condition": "dew_gap", "below": 4}, ctx).passed


def test_stale_data_is_fail_safe():
    ctx = make_context()
    ctx.weather_stations[0].stale = True
    # bare threshold: assume the weather is bad
    assert evaluate({"condition": "wind_speed", "above": 15}, ctx).passed
    # duration threshold (reopen-style): never pass on stale data
    assert not evaluate({"condition": "wind_speed", "below": 15, "for": "1h"}, ctx).passed


def test_duration_threshold_requires_elapsed_time():
    ctx = make_context()
    ctx.weather_stations[0].values["wind_speed"] = 5.0
    memory = TransientMemory()
    cfg = {"condition": "wind_speed", "below": 10, "for": "1h"}

    # first evaluation starts the clock
    assert not evaluate(cfg, ctx, memory).passed
    assert memory.get() is not None

    # pretend the comparison has been holding for two hours
    memory.set(ctx.utcnow() - datetime.timedelta(hours=2))
    result = evaluate(cfg, ctx, memory)
    assert result.passed
    # the timer restarts after a pass
    assert memory.get() == ctx.utcnow()


def test_duration_threshold_resets_when_comparison_breaks():
    ctx = make_context()
    memory = TransientMemory()
    memory.set(ctx.utcnow() - datetime.timedelta(hours=2))
    ctx.weather_stations[0].values["wind_speed"] = 20.0
    assert not evaluate({"condition": "wind_speed", "below": 10, "for": "1h"}, ctx, memory).passed
    assert memory.get() == ctx.utcnow()  # clock restarted


def test_second_station_used_when_first_is_stale():
    ctx = make_context()
    from .fakes import FakeWeatherStation

    ctx.weather_stations[0].stale = True
    ctx.weather_stations.append(FakeWeatherStation(humidity=95.0))
    assert evaluate({"condition": "humidity", "above": 90}, ctx).passed


def test_nan_reading_is_treated_as_unavailable():
    # A station without a given sensor reports NaN (chimera convention);
    # it must be skipped, not silently compared (NaN comparisons are false).
    ctx = make_context()
    from .fakes import FakeWeatherStation

    ctx.weather_stations[0].stale = True
    ctx.weather_stations.append(FakeWeatherStation(humidity=float("nan")))
    # no usable humidity anywhere: bare threshold fail-safes to unsafe
    assert evaluate({"condition": "humidity", "above": 90}, ctx).passed
    # ... and a fresh station AFTER the NaN one is still found
    ctx.weather_stations.append(FakeWeatherStation(humidity=95.0))
    assert evaluate({"condition": "humidity", "above": 90}, ctx).passed
    assert not evaluate({"condition": "humidity", "below": 90}, ctx).passed


def test_threshold_requires_exactly_one_direction():
    with pytest.raises(ConfigError):
        parse_condition({"condition": "humidity", "above": 1, "below": 2}, "test")
    with pytest.raises(ConfigError):
        parse_condition({"condition": "humidity"}, "test")


# ----------------------------------------------------------------------
# flags / operator
# ----------------------------------------------------------------------


def test_flag_is_and_is_not():
    ctx = make_context()
    ctx.flags.set_flag("dome", Flag.READY)
    assert evaluate({"condition": "flag", "instrument": "dome", "is": "ready"}, ctx).passed
    assert evaluate({"condition": "flag", "instrument": "dome", "is_not": "lock"}, ctx).passed
    assert not evaluate({"condition": "flag", "instrument": "dome", "is": "close"}, ctx).passed


def test_flag_accepts_legacy_integer():
    condition = parse_condition({"condition": "flag", "instrument": "dome", "is": 1}, "test")
    assert condition.value == "ready"


def test_flag_lock_keys():
    ctx = make_context()
    ctx.flags.lock("dome", "dew")
    assert evaluate(
        {"condition": "flag", "instrument": "dome", "locked_with_key": "dew"}, ctx
    ).passed
    assert evaluate(
        {"condition": "flag", "instrument": "dome", "not_locked_with_key": "operator"}, ctx
    ).passed


def test_ask_operator():
    ctx = make_context()
    ctx.notifier.answers = ["yes"]
    assert evaluate(
        {"condition": "ask_operator", "question": "Open?", "timeout": "10s"}, ctx
    ).passed
    assert ctx.notifier.questions == ["Open?"]
    # defaults to "no"
    assert not evaluate({"condition": "ask_operator", "question": "Again?"}, ctx).passed


def test_unknown_condition_kind():
    with pytest.raises(ConfigError):
        parse_condition({"condition": "wibble"}, "test")


def test_unknown_key_rejected():
    with pytest.raises(ConfigError):
        parse_condition({"condition": "dome", "slit": "open", "mode": 1}, "test")
