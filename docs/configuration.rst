Checklist configuration reference
=================================

Checklist files are YAML documents in the supervisor's ``checklist_dir``
(default ``~/.chimera/supervisor``). Every ``*.yaml`` file in the directory
is loaded; item names must be unique across all files. Files are validated
as a whole — a file with any error is rejected entirely and reported, never
half-loaded.

File shape
----------

::

    checklist:            # optional wrapper; a bare mapping also works
      <item_name>:
        description: <text>            # optional, shown in logs
        active: true|false             # default true; inactive items only run manually
        run: on_change|always          # default on_change
        on_error: continue|abort       # default continue
        conditions:                    # optional; omit for manual procedures
          - condition: <kind>
            ...
        responses:                     # required, at least one
          - action: <kind>
            ...

Semantics
---------

* Conditions are evaluated in order and ANDed; the first failing condition
  stops the item (so put cheap checks first and ``ask_operator`` last).
  For OR, write two items.
* ``run: on_change`` fires responses when the aggregate status flips from
  false to true; ``run: always`` fires on every cycle while it holds.
* ``on_error: continue`` runs the remaining responses even if one fails
  (close-down lists must try everything); ``abort`` stops at the first
  failure (don't take flats if the dome didn't open).
* Items without ``conditions`` never run automatically; trigger them with
  ``chimera-supervisor run <item>`` or Telegram ``/run <item>`` / ``/list``.

Durations
---------

Anywhere a duration is accepted: ``"90s"``, ``"30m"``, ``"2h"``,
``"1h30m"``, or a bare number — hours for ``offset:``/``for:``, seconds for
``timeout:`` (matching the units the legacy format used).

Conditions
----------

``time`` — before/after a solar event or a fixed UT time::

    - condition: time
      after: sunset            # or before:
      offset: -2h              # optional
    # references: sunset, sunset_twilight_begin, sunset_twilight_end,
    #             sunrise, sunrise_twilight_begin, sunrise_twilight_end,
    #             "HH:MM" (UT)

``dome`` — slit/flap state::

    - condition: dome
      slit: open               # slit: open|closed  or  flap: open|closed

``telescope`` — telescope state::

    - condition: telescope
      state: parked
    # states: parked, unparked, cover_open, cover_closed, slewing,
    #         not_slewing, tracking, not_tracking,
    #         m1_warmer_than_front_ring, m1_cooler_than_front_ring

``weather_station`` — health of one station (0-based index into the
``weatherstations`` list)::

    - condition: weather_station
      station: 2
      state: ok                # ok = fresh data; stale = no fresh data

Weather thresholds — ``humidity`` (%), ``temperature`` (°C), ``wind_speed``
(m/s), ``transparency`` (%), ``dew_point`` (°C), ``dew_gap`` (°C; ambient
temperature minus dew point)::

    - condition: humidity
      above: 85                # exactly one of above:/below:
    - condition: wind_speed
      below: 10
      for: 30m                 # must hold continuously this long

  The first weather station with fresh data (younger than the controller's
  ``max_weather_age``) is used. Fail-safe on stale data: bare thresholds
  pass (assume bad weather), ``for:`` thresholds fail (never reopen on
  stale data). After a ``for:`` threshold passes, its timer restarts.

``flag`` — instrument operation flags and lock keys::

    - condition: flag
      instrument: dome
      is: ready                # is: / is_not: unset|ready|operating|close|lock|error
    - condition: flag
      instrument: dome
      locked_with_key: dew     # or not_locked_with_key:

``ask_operator`` — ask via the notifier (Telegram); passes on "yes"::

    - condition: ask_operator
      question: Open telescope and start robobs?
      timeout: 120s            # default 60s; times out to "no"

Actions
-------

``dome``::

    - action: dome
      do: open_slit            # open_slit|close_slit|open_flap|close_flap|track|stand|slew
    - action: dome
      do: slew
      azimuth: 90              # degrees, or oppose_sun

  ``open_slit``/``open_flap`` are refused unless the flag board allows
  opening (dome and site flags ``ready``/``operating``); closing is always
  attempted, whatever the flags say.

``telescope``::

    - action: telescope
      do: unpark               # unpark|park|open_cover|close_cover|stop_tracking|slew
    - action: telescope
      do: slew
      alt: 80                  # alt:/az: or ra:/dec:
      az: 89

  ``unpark`` refuses when the telescope flag is ``error``; ``park`` sets it
  to ``close``; ``open_cover`` requires opening permission.

``fan`` / ``lamp`` — any chimera switch by location::

    - action: fan
      do: switch_on            # switch_on|switch_off
      fan: /CSKFan/DomeFanEast
      speed: 600               # optional, switch_on only
    - action: lamp
      do: switch_off
      lamp: /SchneiderOTBLamp/building

``set_flag`` / ``lock`` / ``unlock``::

    - action: set_flag
      instrument: site
      flag: ready              # names, not numbers
    - action: lock
      instrument: dome
      key: dew
    - action: unlock
      instrument: dome
      key: dew                 # instrument reopens (flag close) only when
                               # the last key is released

``notify`` / ``send_photo`` / ``ask_operator``::

    - action: notify
      message: Power cut!
    - action: send_photo
      url: http://camera.lan/image.jpg
      message: all-sky now
    - action: ask_operator     # informational; answer is broadcast
      question: Everything looks fine?
      timeout: 120s

``run_script``::

    - action: run_script
      path: /path/to/script.sh   # non-zero exit status counts as failure
      timeout: 10m               # default 10m; the process group is killed
      background: true           # don't block the cycle; outcome is broadcast
      quiet: true                # only notify on failure

On a non-zero exit the script's output (stdout and stderr, tail-truncated) is
broadcast along with the status, so a script can be used as an ad-hoc health
check: put the logic in shell, print the reason, ``exit 1``.  Use
``quiet: true`` for a check that runs every cycle so the operator only hears
about it when it actually fails::

    - action: run_script
      path: /home/astro/bin/check-gps-time.sh
      timeout: 30s
      quiet: true

``scheduler`` / ``robobs`` / ``stop_all`` / ``configure_scheduler``::

    - action: scheduler
      do: start                # start|stop
    - action: robobs
      do: start                # start|stop|wake (start also wakes)
    - action: stop_all         # close scheduler flag, stop robobs,
                               # stop scheduler, stop telescope tracking
    - action: configure_scheduler
      file: /path/to/queue.yaml  # chimera-sched YAML; replaces the queue

Event hooks
-----------

Two item names are special — if defined, their responses run when the
matching chimera event arrives:

* ``on_scheduler_error`` — the scheduler finished a program with an ERROR
  status.
* ``on_object_too_low`` — telescope tracking stopped with
  ``OBJECT_TOO_LOW`` (the 1.x behavior of force-restarting robobs is gone;
  define this item to choose the reaction).
