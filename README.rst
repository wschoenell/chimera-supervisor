chimera-supervisor
==================

Observatory supervisor plugin for the chimera_ observatory control system:
a configurable checklist of **conditions → responses** that opens and closes
the observatory, enforces weather and safety locks, and talks to the
operators through Telegram.

This is version 2.0, a full refactor targeting chimera 0.2 / Python 3.13.
It was rewritten from the Python-2 original; the checklist configuration
format changed from numeric ``mode`` codes to a human-readable one (a
converter is included, see *Migrating* below).

The robotic-observation subsystem (RobObs) that used to live in this package
was extracted to its own repository, chimera-robobs_. The supervisor can
still start/stop/wake a robobs controller — point the ``robobs`` config key
at its location and use ``action: robobs`` — without any code dependency.

.. _chimera-robobs: https://github.com/astroufsc/chimera-robobs

.. _chimera: https://github.com/astroufsc/chimera

Concepts
--------

* A **checklist item** has *conditions* (ANDed; e.g. "after sunset − 2h",
  "dome slit closed", "site flag is ready") and *responses* (e.g. "unpark
  telescope", "open dome slit", "switch fan on").
* Items normally fire when their conditions *become* true (``run:
  on_change``); ``run: always`` fires on every cycle while they hold.
* ``on_error: continue`` (default) keeps running the remaining responses if
  one fails — right for close-down sequences; ``on_error: abort`` stops at
  the first failure — right for open-up sequences.
* Items **without conditions** are manual procedures, triggered with
  ``chimera-supervisor run <item>`` or Telegram ``/run``.
* Instruments carry an **operation flag** (``unset / ready / operating /
  close / lock / error``). Locks are held by named keys; every key must be
  released before a locked instrument can operate again (e.g. a ``dew`` lock
  and an ``operator`` lock must both be lifted).
* Weather conditions are **fail-safe**: when no weather station has fresh
  data, "is it unsafe?" thresholds pass and "can we reopen?" (``for:``)
  thresholds fail.

Configuration
-------------

Checklist files are plain YAML in ``checklist_dir`` (default
``~/.chimera/supervisor``), loaded at start and on ``reload`` — there is no
database-loading step anymore. Print a commented example with::

    chimera-supervisor example

A taste::

    checklist:
      close_on_dew:
        description: Close everything when close to condensation
        conditions:
          - condition: dome
            slit: open
          - condition: dew_gap     # ambient temperature minus dew point
            below: 4               # degrees Celsius
        responses:
          - action: stop_all
          - action: telescope
            do: close_cover
          - action: dome
            do: close_slit
          - action: lock
            instrument: dome
            key: dew

See ``docs/configuration.rst`` for the full catalog of conditions and
actions, ``docs/DESIGN.md`` for the architecture and the reasoning behind
the 2.0 changes (including the SQLAlchemy trade-off), and
``docs/deploy-fake-observatory.md`` for a verified recipe to soak-test the
whole stack against fake hardware on a remote server.

The controller is declared in ``chimera.config`` like any other::

    controllers:
      - type: Supervisor
        name: supervisor
        telescope: /Telescope/0
        dome: /Dome/0
        scheduler: /Scheduler/0
        weatherstations: /WeatherStation/ws1,/WeatherStation/ws2
        checklist_dir: ~/.chimera/supervisor
        telegram_token: "123456:ABC..."
        telegram_broadcast_ids: "-100123,-100456"
        telegram_listen_ids: "111,222"

Command line
------------

Offline (no running chimera needed)::

    chimera-supervisor validate <file-or-dir>...     # check configs
    chimera-supervisor migrate <legacy.yaml> -o DIR  # convert old configs
    chimera-supervisor example                       # print an example

Online::

    chimera-supervisor info | items | run <item> | reload | wakeup
    chimera-supervisor lock <instrument> <key> | unlock <instrument> <key>
    chimera-supervisor start | stop

Migrating from 1.x
------------------

1. Convert your checklist files: ``chimera-supervisor migrate old/*.yaml -o
   ~/.chimera/supervisor`` (every numeric mode becomes words; the tool
   refuses anything it cannot map). A complete real-world example — the
   T80-South production set, converted and curated — lives in
   ``examples/t80s/``.
2. Update the controller config keys: ``telegram-token`` →
   ``telegram_token``, ``telegram-broascast-ids`` →
   ``telegram_broadcast_ids`` (typo fixed), ``telegram-listen-ids`` →
   ``telegram_listen_ids``, ``max_mins`` → ``max_weather_age``; add
   ``checklist_dir``.
3. The old ``manager_checklist.db`` / ``manager_status.db`` are not used
   anymore; runtime state lives in ``supervisor_state.db`` (instrument
   flags, lock keys, item status). Flags start over as ``unset``.

Development
-----------

::

    uv sync
    uv run pytest
    uv run ruff check

The test suite validates the converter against the full set of T80-South
production configs (set ``T80S_SUPERVISOR_DIR`` to point at them).

License
-------

GPL v2, see ``licenses/``.
