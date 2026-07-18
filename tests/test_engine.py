# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

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
