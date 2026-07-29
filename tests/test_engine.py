# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import datetime

import yaml

from chimera_supervisor.core import checklist
from chimera_supervisor.core.engine import Engine

from .fakes import make_context


def make_engine(document, ctx=None):
    ctx = ctx or make_context()
    engine = Engine(ctx=ctx, store=ctx.flags)
    engine.load(checklist.parse_document(yaml.safe_load(document), "test"))
    return engine, ctx


CLOSE_ON_DEW = """
close_on_dew:
  conditions:
    - condition: dome
      slit: open
    - condition: dew_gap
      below: 4
  responses:
    - action: dome
      do: close_slit
    - action: lock
      instrument: dome
      key: dew
"""


def test_responses_fire_only_on_status_change():
    engine, ctx = make_engine(CLOSE_ON_DEW)
    dome = ctx.domes[0]
    dome.slit_open = True
    ctx.weather_stations[0].values.update(temperature=10.0, dew_point=8.0)

    engine.run_cycle()
    assert dome.calls == ["close_slit"]
    assert ctx.flags.has_key("dome", "dew")

    # next cycle: conditions now fail (slit closed) -> status flips back
    engine.run_cycle()
    assert dome.calls == ["close_slit"]

    # dome reopened somehow, dew still bad -> fires again (status changed)
    dome.slit_open = True
    engine.run_cycle()
    assert dome.calls == ["close_slit", "close_slit"]


def test_short_circuit_stops_at_first_failed_condition():
    engine, ctx = make_engine(CLOSE_ON_DEW)
    ctx.domes[0].slit_open = False  # first condition fails
    ctx.notifier.answers = []  # would explode if later conditions ran with asks
    engine.run_cycle()
    assert ctx.domes[0].calls == []


def test_run_always_fires_every_cycle():
    document = """
keep_closing:
  run: always
  conditions:
    - condition: dome
      slit: open
  responses:
    - action: notify
      message: closing again
"""
    engine, ctx = make_engine(document)
    ctx.domes[0].slit_open = True
    engine.run_cycle()
    engine.run_cycle()
    assert ctx.notifier.messages.count("closing again") == 2


def test_on_error_abort_stops_response_list():
    document = """
open_up:
  on_error: abort
  conditions:
    - condition: time
      after: sunset
  responses:
    - action: dome
      do: open_slit
    - action: notify
      message: should not happen
"""
    engine, ctx = make_engine(document)
    # site flag UNSET -> open_slit raises ActionError -> abort
    engine.run_cycle()
    assert "should not happen" not in ctx.notifier.messages


def test_on_error_continue_runs_remaining_responses():
    document = """
close_down:
  conditions:
    - condition: time
      after: sunset
  responses:
    - action: robobs
      do: start
    - action: notify
      message: kept going
"""
    engine, ctx = make_engine(document)
    ctx.robobs.clear()  # robobs action fails: none configured
    engine.run_cycle()
    assert "kept going" in ctx.notifier.messages


def test_inactive_items_are_skipped_but_runnable_manually():
    document = """
park:
  active: false
  conditions:
    - condition: time
      after: sunset
  responses:
    - action: telescope
      do: park
"""
    engine, ctx = make_engine(document)
    ctx.telescopes[0].parked = False
    engine.run_cycle()
    assert not ctx.telescopes[0].parked

    assert "park" in engine.manual_items()
    assert engine.run_action("park") is True
    assert ctx.telescopes[0].parked


def test_manual_item_without_conditions_never_autoruns():
    document = """
procedure:
  responses:
    - action: notify
      message: manual only
"""
    engine, ctx = make_engine(document)
    engine.run_cycle()
    assert "manual only" not in ctx.notifier.messages
    assert engine.run_action("procedure")
    assert "manual only" in ctx.notifier.messages


def test_menu_hides_marked_items_and_event_hooks():
    document = """
procedure:
  responses:
    - action: notify
      message: hi
recovery:
  menu: false
  responses:
    - action: notify
      message: recover
on_scheduler_error:
  responses:
    - action: notify
      message: program failed
"""
    engine, ctx = make_engine(document)
    # the hook is not an operator procedure at all; `recovery` is one, it is
    # just kept off the buttons
    assert engine.manual_items() == ["procedure", "recovery"]
    assert engine.menu_items() == ["procedure"]
    # and nothing here runs by itself
    engine.run_cycle()
    assert ctx.notifier.messages == []
    assert engine.run_action("on_scheduler_error")
    assert "program failed" in ctx.notifier.messages


def test_run_action_unknown_item():
    engine, _ = make_engine("x:\n  responses:\n    - action: stop_all\n")
    assert engine.run_action("nope") is False


def test_duration_condition_state_survives_engine_reload():
    document = """
reopen:
  conditions:
    - condition: wind_speed
      below: 10
      for: 1h
  responses:
    - action: unlock
      instrument: dome
      key: wind
"""
    engine, ctx = make_engine(document)
    ctx.weather_stations[0].values["wind_speed"] = 5.0
    engine.run_cycle()  # starts the clock, persisted in the store

    # simulate a restart: new engine over the same store
    engine2 = Engine(ctx=ctx, store=ctx.flags)
    engine2.load(checklist.parse_document(yaml.safe_load(document), "test"))
    assert ctx.flags.get_since("reopen", 0) is not None


def test_condition_crash_counts_as_failed():
    engine, ctx = make_engine(CLOSE_ON_DEW)
    ctx.domes[0].slit_open = True
    ctx.weather_stations.clear()  # dew_gap: no stations -> fail-safe True? no:
    # bare threshold with no stations at all -> stale rule (True); make the
    # dome check crash instead to exercise the exception path
    ctx.domes.clear()
    engine.run_cycle()  # must not raise
    assert ctx.flags.item_status("close_on_dew") is False


GATED_FOR = """
close_on_transparency_lock:
  conditions:
    - condition: dome
      slit: open
    - condition: transparency
      below: 40
      for: 30m
  responses:
    - action: dome
      do: close_slit
"""


def advance(ctx, **delta):
    """Move the site clock (and the stations stamped against it) forward."""
    later = ctx.site.ut() + datetime.timedelta(**delta)
    ctx.site._ut = later
    for station in ctx.weather_stations:
        station._ut = later


def test_for_timer_does_not_accumulate_behind_a_failing_condition():
    """opd-40 2026-07-25: `close_on_transparency_lock` fired ~1 min after the
    sky went bad because its `for: 30m` sat behind a flag gate and had not
    been evaluated - so its `since` was 6 h stale and the debounce was spent
    the instant the gate opened."""
    engine, ctx = make_engine(GATED_FOR)
    dome = ctx.domes[0]
    ctx.weather_stations[0].values["sky_transparency"] = 33.0

    # an earlier bad-sky episode with the gate OPEN arms the timer
    dome.slit_open = True
    engine.run_cycle()
    assert dome.calls == []
    assert ctx.flags.get_since("close_on_transparency_lock", 1) is not None

    # then the gate shuts for six hours - the armed timer must not keep aging
    dome.slit_open = False
    engine.run_cycle()
    advance(ctx, hours=6)
    engine.run_cycle()

    # gate opens again: the debounce starts NOW, it must not already be spent
    dome.slit_open = True
    engine.run_cycle()
    assert dome.calls == []

    # and it still has to run the full 30 m
    advance(ctx, minutes=29)
    engine.run_cycle()
    assert dome.calls == []

    advance(ctx, minutes=2)
    engine.run_cycle()
    assert dome.calls == ["close_slit"]
