# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import pytest

from chimera_supervisor.core.actions import parse_action
from chimera_supervisor.core.exceptions import ActionError, ConfigError
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag

from .fakes import make_context


def run(cfg, ctx):
    parse_action(cfg, "test").execute(ctx)


def ready_site(ctx):
    ctx.flags.set_flag("site", Flag.READY)


def test_dome_open_slit_requires_permission():
    ctx = make_context()
    # site UNSET -> can_open is False
    with pytest.raises(ActionError):
        run({"action": "dome", "do": "open_slit"}, ctx)
    assert not ctx.domes[0].slit_open

    ready_site(ctx)
    ctx.flags.set_flag("dome", Flag.READY)
    run({"action": "dome", "do": "open_slit"}, ctx)
    assert ctx.domes[0].slit_open
    assert ctx.flags.get_flag("dome") == Flag.OPERATING


def test_dome_close_slit_always_closes():
    ctx = make_context()
    ctx.domes[0].slit_open = True
    ctx.flags.set_flag("dome", Flag.OPERATING)
    run({"action": "dome", "do": "close_slit"}, ctx)
    assert not ctx.domes[0].slit_open
    assert ctx.flags.get_flag("dome") == Flag.READY


def test_dome_slew_to_azimuth():
    ctx = make_context()
    run({"action": "dome", "do": "slew", "azimuth": 90}, ctx)
    assert ctx.domes[0].azimuth == 90.0


def test_dome_slew_opposes_sun():
    ctx = make_context()
    ctx.site.sun_alt_az = (-10.0, 60.0)
    run({"action": "dome", "do": "slew", "azimuth": "oppose_sun"}, ctx)
    assert ctx.domes[0].azimuth == 240.0


def test_telescope_park_sets_close_flag():
    ctx = make_context()
    ctx.telescopes[0].parked = False
    run({"action": "telescope", "do": "park"}, ctx)
    assert ctx.telescopes[0].parked
    assert ctx.flags.get_flag("telescope") == Flag.CLOSE


def test_telescope_unpark_refuses_error_flag():
    ctx = make_context()
    ctx.flags.set_flag("telescope", Flag.ERROR)
    with pytest.raises(ActionError):
        run({"action": "telescope", "do": "unpark"}, ctx)
    assert ctx.telescopes[0].parked


def test_telescope_unpark():
    ctx = make_context()
    ctx.flags.set_flag("telescope", Flag.CLOSE)
    run({"action": "telescope", "do": "unpark"}, ctx)
    assert not ctx.telescopes[0].parked
    assert ctx.flags.get_flag("telescope") == Flag.READY


def test_telescope_open_cover_requires_permission():
    ctx = make_context()
    with pytest.raises(ActionError):
        run({"action": "telescope", "do": "open_cover"}, ctx)
    ready_site(ctx)
    ctx.flags.set_flag("telescope", Flag.READY)
    run({"action": "telescope", "do": "open_cover"}, ctx)
    assert ctx.telescopes[0].cover_open


def test_fan_on_with_speed():
    ctx = make_context()
    run({"action": "fan", "do": "switch_on", "fan": "/Fan/east", "speed": 600}, ctx)
    fan = ctx.resolve("/Fan/east")
    assert fan.on and fan.rotation == 600


def test_fan_off():
    ctx = make_context()
    ctx.resolve("/Fan/east").on = True
    run({"action": "fan", "do": "switch_off", "fan": "/Fan/east"}, ctx)
    assert not ctx.resolve("/Fan/east").on


def test_lamp_on_off():
    ctx = make_context()
    run({"action": "lamp", "do": "switch_on", "lamp": "/Lamp/dome"}, ctx)
    assert ctx.resolve("/Lamp/dome").on
    run({"action": "lamp", "do": "switch_off", "lamp": "/Lamp/dome"}, ctx)
    assert not ctx.resolve("/Lamp/dome").on


def test_set_flag_by_name_and_legacy_int():
    ctx = make_context()
    run({"action": "set_flag", "instrument": "site", "flag": "ready"}, ctx)
    assert ctx.flags.get_flag("site") == Flag.READY
    run({"action": "set_flag", "instrument": "site", "flag": 3}, ctx)
    assert ctx.flags.get_flag("site") == Flag.CLOSE


def test_lock_unlock_round_trip():
    ctx = make_context()
    run({"action": "lock", "instrument": "dome", "key": "dew"}, ctx)
    assert ctx.flags.has_key("dome", "dew")
    run({"action": "unlock", "instrument": "dome", "key": "dew"}, ctx)
    assert ctx.flags.get_flag("dome") == Flag.CLOSE


def test_unlock_with_remaining_keys_does_not_raise():
    ctx = make_context()
    ctx.flags.lock("dome", "dew")
    ctx.flags.lock("dome", "operator")
    # partial release is not an ActionError (legacy tolerated it)
    run({"action": "unlock", "instrument": "dome", "key": "dew"}, ctx)
    assert ctx.flags.get_flag("dome") == Flag.LOCK


def test_notify_and_send_photo():
    ctx = make_context()
    run({"action": "notify", "message": "hello"}, ctx)
    assert "hello" in ctx.notifier.messages
    run({"action": "send_photo", "url": "http://x/y.jpg", "message": "cam"}, ctx)
    assert ctx.notifier.photos == [("http://x/y.jpg", "cam")]


def test_run_script(tmp_path):
    ctx = make_context()
    marker = tmp_path / "ran"
    script = tmp_path / "do.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    run({"action": "run_script", "path": str(script)}, ctx)
    assert marker.exists()


def test_run_script_missing_or_failing(tmp_path):
    ctx = make_context()
    with pytest.raises(ActionError):
        run({"action": "run_script", "path": str(tmp_path / "nope.sh")}, ctx)
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    with pytest.raises(ActionError):
        run({"action": "run_script", "path": str(bad)}, ctx)


def test_run_script_timeout_kills_hung_script(tmp_path):
    ctx = make_context()
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 60\n")
    slow.chmod(0o755)
    with pytest.raises(ActionError, match="timed out"):
        run({"action": "run_script", "path": str(slow), "timeout": "1s"}, ctx)
    assert any("killed after 1s" in m for m in ctx.notifier.messages)


def test_run_script_timeout_must_be_positive():
    with pytest.raises(ConfigError):
        parse_action({"action": "run_script", "path": "/x.sh", "timeout": 0}, "test")


def test_run_script_background_does_not_block(tmp_path):
    import time

    ctx = make_context()
    marker = tmp_path / "ran"
    script = tmp_path / "bg.sh"
    script.write_text(f"#!/bin/sh\nsleep 0.3\ntouch {marker}\n")
    script.chmod(0o755)
    t0 = time.monotonic()
    run({"action": "run_script", "path": str(script), "background": True}, ctx)
    assert time.monotonic() - t0 < 0.25  # returned before the script finished
    assert not marker.exists()
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists()


def test_run_script_background_reports_failure(tmp_path):
    import time

    ctx = make_context()
    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    run({"action": "run_script", "path": str(bad), "background": True}, ctx)
    for _ in range(50):
        if any("status 3" in m for m in ctx.notifier.messages):
            break
        time.sleep(0.1)
    assert any("status 3" in m for m in ctx.notifier.messages)


def test_run_script_failure_reports_output(tmp_path):
    """A check script's own message is what tells the operator what is wrong."""
    ctx = make_context()
    bad = tmp_path / "check.sh"
    bad.write_text("#!/bin/sh\necho 'clock is NOT on the GPS reference' >&2\nexit 1\n")
    bad.chmod(0o755)
    with pytest.raises(ActionError):
        run({"action": "run_script", "path": str(bad)}, ctx)
    assert any("clock is NOT on the GPS reference" in m for m in ctx.notifier.messages)


def test_run_script_quiet_only_speaks_on_failure(tmp_path):
    ctx = make_context()
    ok = tmp_path / "ok.sh"
    ok.write_text("#!/bin/sh\necho fine\nexit 0\n")
    ok.chmod(0o755)
    run({"action": "run_script", "path": str(ok), "quiet": True}, ctx)
    assert ctx.notifier.messages == []  # nothing at all on a passing check

    bad = tmp_path / "bad.sh"
    bad.write_text("#!/bin/sh\necho broken\nexit 2\n")
    bad.chmod(0o755)
    with pytest.raises(ActionError):
        run({"action": "run_script", "path": str(bad), "quiet": True}, ctx)
    assert any("broken" in m and "status 2" in m for m in ctx.notifier.messages)


def test_run_script_output_is_truncated(tmp_path):
    ctx = make_context()
    noisy = tmp_path / "noisy.sh"
    noisy.write_text("#!/bin/sh\nyes abcdefgh | head -2000\nexit 1\n")
    noisy.chmod(0o755)
    with pytest.raises(ActionError):
        run({"action": "run_script", "path": str(noisy), "quiet": True}, ctx)
    (message,) = [m for m in ctx.notifier.messages if "status 1" in m]
    assert "[...]" in message
    assert len(message) < 1500  # tail only, keeps notifications sane


def test_run_script_config_roundtrip():
    action = parse_action(
        {
            "action": "run_script",
            "path": "/x.sh",
            "timeout": "5m",
            "background": True,
            "quiet": True,
        },
        "test",
    )
    assert action.to_config() == {
        "action": "run_script",
        "path": "/x.sh",
        "timeout": "5m",
        "background": True,
        "quiet": True,
    }
    # defaults stay out of the config (legacy converter emits the bare form)
    assert parse_action(
        {"action": "run_script", "path": "/x.sh"}, "test"
    ).to_config() == {
        "action": "run_script",
        "path": "/x.sh",
    }


def test_scheduler_start_stop():
    ctx = make_context()
    run({"action": "scheduler", "do": "start"}, ctx)
    assert ctx.schedulers[0].calls == ["start"]
    assert ctx.flags.get_flag("scheduler") == Flag.OPERATING
    run({"action": "scheduler", "do": "stop"}, ctx)
    assert ctx.schedulers[0].calls == ["start", "stop"]


def test_robobs_start_wakes_it():
    ctx = make_context()
    run({"action": "robobs", "do": "start"}, ctx)
    assert ctx.robobs[0].calls == ["start", "wake"]
    assert ctx.flags.get_flag("robobs") == Flag.OPERATING
    run({"action": "robobs", "do": "stop"}, ctx)
    assert ctx.robobs[0].calls[-1] == "stop"
    assert ctx.flags.get_flag("robobs") == Flag.READY


def test_stop_all_stops_everything_despite_errors():
    ctx = make_context()
    ctx.telescopes[0].tracking = True
    ctx.flags.lock("scheduler", "x")  # makes set_flag(scheduler) fail
    run({"action": "stop_all"}, ctx)
    assert ctx.robobs[0].calls == ["stop"]
    assert ctx.schedulers[0].calls == ["stop"]
    assert not ctx.telescopes[0].tracking


def test_ask_operator_action_broadcasts_answer():
    ctx = make_context()
    ctx.notifier.answers = ["yes"]
    run({"action": "ask_operator", "question": "All good?"}, ctx)
    assert ctx.notifier.questions == ["All good?"]
    assert any("yes" in message for message in ctx.notifier.messages)


def test_unknown_action_and_bad_keys():
    with pytest.raises(ConfigError):
        parse_action({"action": "wibble"}, "test")
    with pytest.raises(ConfigError):
        parse_action({"action": "dome", "do": "open_slit", "mode": 1}, "test")
    with pytest.raises(ConfigError):
        parse_action({"action": "dome", "do": "slew"}, "test")  # missing azimuth
    with pytest.raises(ConfigError):
        parse_action(
            {"action": "fan", "do": "switch_off", "fan": "/f", "speed": 1}, "test"
        )


def test_robobs_start_refuses_while_dome_locked():
    """A locked dome or site is somebody's explicit "do not open".

    `run RobobsStart` executes responses without the checklist's
    conditions, and on 2026-07-22 it started the whole night against a
    closed, operator-locked dome - autofocus on shut-dome darkness and
    science slews to nowhere. The action itself must respect the locks.
    """
    ctx = make_context()
    ctx.flags.lock("dome", "operator")
    run({"action": "robobs", "do": "start"}, ctx)
    assert ctx.robobs[0].calls == [], "robobs started against a locked dome"
    assert ctx.flags.get_flag("robobs") != Flag.OPERATING
    assert any("NOT starting robobs" in m for m in ctx.notifier.messages)

    # site lock refuses too
    ctx2 = make_context()
    ctx2.flags.lock("site", "transparency")
    run({"action": "robobs", "do": "start"}, ctx2)
    assert ctx2.robobs[0].calls == []

    # released: starts normally
    ctx.flags.unlock("dome", "operator")
    run({"action": "robobs", "do": "start"}, ctx)
    assert ctx.robobs[0].calls == ["start", "wake"]
