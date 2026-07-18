# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Legacy-format conversion, validated against the real T80-South configs."""

import os
import pathlib

import pytest
import yaml

from chimera_supervisor.core import checklist, legacy

# the production files (a working copy also lives in this repo's untracked
# supervisor/ directory); override with T80S_SUPERVISOR_DIR
T80S_DIR = pathlib.Path(
    os.environ.get("T80S_SUPERVISOR_DIR", "~/workspace/t80s_scripts/supervisor")
).expanduser()

# scratch fragment, not a real config (corrupted "hecklist:" keys)
T80S_EXCLUDE = {"supervisor_missing.yaml"}


def convert_check(cfg):
    return legacy.convert_check(cfg, [])


def convert_response(cfg):
    return legacy.convert_response(cfg, [])


# ----------------------------------------------------------------------
# check conversions (every mode in production use)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (0, {"condition": "time", "after": "sunset"}),
        (1, {"condition": "time", "after": "sunset"}),
        (-1, {"condition": "time", "before": "sunset"}),
        (2, {"condition": "time", "after": "sunset_twilight_begin"}),
        (3, {"condition": "time", "after": "sunset_twilight_end"}),
        (-3, {"condition": "time", "before": "sunset_twilight_end"}),
        (4, {"condition": "time", "after": "sunrise"}),
        (5, {"condition": "time", "after": "sunrise_twilight_begin"}),
        (-5, {"condition": "time", "before": "sunrise_twilight_begin"}),
        (6, {"condition": "time", "after": "sunrise_twilight_end"}),
    ],
)
def test_check_time_modes(mode, expected):
    assert convert_check({"type": "CheckTime", "mode": mode}) == expected


def test_check_time_delta_becomes_offset():
    assert convert_check({"type": "CheckTime", "mode": 1, "deltaTime": -0.5}) == {
        "condition": "time",
        "after": "sunset",
        "offset": "-30m",
    }
    assert convert_check({"type": "CheckTime", "mode": -1, "deltaTime": 0.2})["offset"] == "12m"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (0, {"condition": "dome", "slit": "open"}),
        (1, {"condition": "dome", "slit": "closed"}),
        (2, {"condition": "dome", "flap": "open"}),
        (3, {"condition": "dome", "flap": "closed"}),
    ],
)
def test_check_dome_modes(mode, expected):
    assert convert_check({"type": "CheckDome", "mode": mode}) == expected


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        (1, "parked"),
        (-1, "unparked"),
        (2, "cover_open"),
        (-2, "cover_closed"),
        (3, "slewing"),
        (-3, "not_slewing"),
        (4, "tracking"),
        (-4, "not_tracking"),
        (5, "m1_warmer_than_front_ring"),
        (-5, "m1_cooler_than_front_ring"),
    ],
)
def test_check_telescope_modes(mode, state):
    assert convert_check({"type": "CheckTelescope", "mode": mode}) == {
        "condition": "telescope",
        "state": state,
    }


def test_check_weather_station():
    assert convert_check({"type": "CheckWeatherStation", "index": 2}) == {
        "condition": "weather_station",
        "station": 2,
        "state": "ok",
    }
    assert convert_check({"type": "CheckWeatherStation", "index": 2, "mode": 1})["state"] == "stale"


@pytest.mark.parametrize(
    ("legacy_type", "value_key", "mode0", "mode1"),
    [
        ("CheckHumidity", "humidity", "above", "below"),
        ("CheckTemperature", "temperature", "below", "above"),
        ("CheckWindSpeed", "windspeed", "above", "below"),
        ("CheckTransparency", "transparency", "below", "above"),
        ("CheckDew", "tempdiff", "below", "above"),
    ],
)
def test_weather_threshold_directions(legacy_type, value_key, mode0, mode1):
    bare = convert_check({"type": legacy_type, value_key: 42})
    assert bare[mode0] == 42.0 and "for" not in bare

    timed = convert_check({"type": legacy_type, value_key: 42, "mode": 1, "deltaTime": 1})
    assert timed[mode1] == 42.0 and timed["for"] == "1h"


def test_mode1_with_zero_delta_keeps_for_key():
    # for: 0h must stay: it preserves the legacy fail-safe on stale data
    timed = convert_check({"type": "CheckTransparency", "transparency": 35, "mode": 1})
    assert timed["for"] == "0h"


def test_ask_listener():
    assert convert_check(
        {"type": "AskListener", "question": "Open?", "waittime": 120}
    ) == {"condition": "ask_operator", "question": "Open?", "timeout": "120s"}


@pytest.mark.parametrize(
    ("mode", "test_key"),
    [(0, "is"), (1, "is_not"), (2, "locked_with_key"), (3, "not_locked_with_key")],
)
def test_instrument_flag_modes(mode, test_key):
    value = "READY" if mode in (0, 1) else "dew"
    converted = convert_check(
        {"type": "CheckInstrumentFlag", "instrument": "dome", "flag": value, "mode": mode}
    )
    expected = "ready" if mode in (0, 1) else "dew"
    assert converted == {"condition": "flag", "instrument": "dome", test_key: expected}


# ----------------------------------------------------------------------
# response conversions
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (0, {"action": "dome", "do": "open_slit"}),
        (1, {"action": "dome", "do": "close_slit"}),
        (2, {"action": "dome", "do": "open_flap"}),
        (3, {"action": "dome", "do": "close_flap"}),
        (9, {"action": "dome", "do": "track"}),
        (10, {"action": "dome", "do": "stand"}),
    ],
)
def test_dome_action_simple_modes(mode, expected):
    assert convert_response({"type": "DomeAction", "mode": mode}) == expected


def test_dome_action_slew_and_fans_and_lamps():
    assert convert_response({"type": "DomeAction", "mode": 4, "parameter": 90}) == {
        "action": "dome",
        "do": "slew",
        "azimuth": 90.0,
    }
    assert convert_response({"type": "DomeAction", "mode": 4, "parameter": "oppose-sun"}) == {
        "action": "dome",
        "do": "slew",
        "azimuth": "oppose_sun",
    }
    assert convert_response(
        {"type": "DomeAction", "mode": 5, "parameter": "/CSKFan/DomeFanEast,600"}
    ) == {"action": "fan", "do": "switch_on", "fan": "/CSKFan/DomeFanEast", "speed": 600.0}
    assert convert_response(
        {"type": "DomeAction", "mode": 6, "parameter": "/CSKFan/DomeFanWest"}
    ) == {"action": "fan", "do": "switch_off", "fan": "/CSKFan/DomeFanWest"}
    assert convert_response(
        {"type": "DomeAction", "mode": 8, "parameter": "/SchneiderOTBLamp/building"}
    ) == {"action": "lamp", "do": "switch_off", "lamp": "/SchneiderOTBLamp/building"}


def test_telescope_action_modes():
    assert convert_response({"type": "TelescopeAction", "mode": 0}) == {
        "action": "telescope",
        "do": "unpark",
    }
    assert convert_response({"type": "TelescopeAction", "mode": 3}) == {
        "action": "telescope",
        "do": "close_cover",
    }
    assert convert_response(
        {"type": "TelescopeAction", "mode": 9, "parameter": "/SchneiderOTBFan/M1"}
    ) == {"action": "fan", "do": "switch_off", "fan": "/SchneiderOTBFan/M1"}
    assert convert_response(
        {"type": "TelescopeAction", "mode": 5, "parameter": "80,89"}
    ) == {"action": "telescope", "do": "slew", "alt": 80.0, "az": 89.0}


def test_set_flag_uses_names_not_numbers():
    assert convert_response(
        {"type": "SetInstrumentFlag", "instrument": "site", "flag": 1}
    ) == {"action": "set_flag", "instrument": "site", "flag": "ready"}
    assert convert_response(
        {"type": "SetInstrumentFlag", "instrument": "telescope", "flag": 3}
    ) == {"action": "set_flag", "instrument": "telescope", "flag": "close"}


def test_lock_unlock_and_case_typo():
    assert convert_response(
        {"type": "LockInstrument", "instrument": "dome", "key": "dew"}
    ) == {"action": "lock", "instrument": "dome", "key": "dew"}
    # one production file spells it "UnLockInstrument"
    assert convert_response(
        {"type": "UnLockInstrument", "instrument": "dome", "key": "dew"}
    ) == {"action": "unlock", "instrument": "dome", "key": "dew"}


def test_misc_responses():
    assert convert_response({"type": "SendTelegram", "message": "hi"}) == {
        "action": "notify",
        "message": "hi",
    }
    assert convert_response({"type": "SendPhoto", "path": "http://cam/x.jpg"}) == {
        "action": "send_photo",
        "url": "http://cam/x.jpg",
    }
    assert convert_response({"type": "ExecuteScript", "filename": "/x.sh"}) == {
        "action": "run_script",
        "path": "/x.sh",
    }
    assert convert_response({"type": "ConfigureScheduler", "filename": "/q.yaml"}) == {
        "action": "configure_scheduler",
        "file": "/q.yaml",
    }
    assert convert_response({"type": "StartRobObs"}) == {"action": "robobs", "do": "start"}
    assert convert_response({"type": "StopScheduler"}) == {"action": "scheduler", "do": "stop"}
    assert convert_response({"type": "StopAll"}) == {"action": "stop_all"}


def test_unknown_types_raise():
    with pytest.raises(legacy.LegacyConversionError):
        convert_check({"type": "CheckWibble"})
    with pytest.raises(legacy.LegacyConversionError):
        convert_response({"type": "DoWibble"})
    with pytest.raises(legacy.LegacyConversionError):
        convert_response({"type": "DomeAction", "mode": 42})


# ----------------------------------------------------------------------
# item-level flags
# ----------------------------------------------------------------------


def test_item_level_conversion():
    name, item = legacy.convert_item(
        {
            "name": "OpenAtSunset",
            "comment": "Open the dome",
            "eager": True,
            "eager_response": False,
            "active": False,
            "check": [{"type": "CheckTime", "mode": 1}],
            "responses": [{"type": "DomeAction", "mode": 0}],
        },
        "test",
        [],
    )
    assert name == "OpenAtSunset"
    assert item["description"] == "Open the dome"
    assert item["run"] == "always"  # eager
    assert item["on_error"] == "abort"  # eager_response: False
    assert item["active"] is False
    assert item["conditions"] == [{"condition": "time", "after": "sunset"}]
    assert item["responses"] == [{"action": "dome", "do": "open_slit"}]


def test_item_defaults_are_omitted():
    _, item = legacy.convert_item(
        {
            "name": "X",
            "eager": False,
            "check": [{"type": "CheckDome", "mode": 0}],
            "responses": [{"type": "StopAll"}],
        },
        "test",
        [],
    )
    assert "run" not in item and "on_error" not in item and "active" not in item


# ----------------------------------------------------------------------
# the real T80-South production corpus
# ----------------------------------------------------------------------

t80s_files = (
    sorted(
        path
        for path in T80S_DIR.glob("*.yaml")
        if path.name not in T80S_EXCLUDE and not path.name.startswith(".")
    )
    if T80S_DIR.is_dir()
    else []
)


@pytest.mark.skipif(not t80s_files, reason="T80S supervisor configs not available")
@pytest.mark.parametrize("path", t80s_files, ids=lambda p: p.name)
def test_every_t80s_file_converts_and_reparses(path):
    items, converted, _ = legacy.convert_file(path)
    assert items, f"{path} produced no items"

    # structure preserved: same number of items / conditions / responses
    original = yaml.safe_load(path.read_text())["checklist"]
    assert len(items) == len(original)
    for item, old in zip(items, original):
        assert len(item.conditions) == len(old.get("check") or [])
        assert len(item.responses) == len(old.get("responses") or [])

    # migrated output must round-trip through the new parser
    dumped = checklist.dump_items(items)
    reparsed = checklist.parse_document(yaml.safe_load(dumped), str(path))
    assert [i.name for i in reparsed] == [i.name for i in items]
    for old_item, new_item in zip(items, reparsed):
        assert old_item.conditions == new_item.conditions
        assert old_item.responses == new_item.responses
