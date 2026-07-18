# chimera-supervisor 2.0 — refactor design

Date: 2026-07-06. Target: chimera 0.2 (Python ≥ 3.13, snake_case API, pynng bus).

## Goals

1. Human-readable configuration (no numeric `mode` codes), validated against the
   T80-South production configs in use today.
2. Separation of concerns: controller / checklist engine / condition & action
   catalog / persistence / Telegram bot each in their own module.
3. Configuration lives in YAML files, not in a database. Only *runtime state*
   is persisted.
4. Tests for everything that can be tested without hardware.
5. A deliberate decision on SQLAlchemy, and a reorganized robobs DB schema.

## Package layout (template-conformant, src/ layout)

```
src/chimera_supervisor/
├── controllers/
│   └── supervisor.py      # Supervisor(ChimeraObject): lifecycle, events, public API only
├── core/
│   ├── conditions.py      # condition catalog (one class per condition, registry)
│   ├── actions.py         # action catalog (one class per action, registry)
│   ├── engine.py          # checklist engine: evaluate conditions, fire actions
│   ├── checklist.py       # ChecklistItem dataclass + load/validate from YAML
│   ├── legacy.py          # legacy (mode-number) format reader + converter
│   ├── flags.py           # InstrumentOperationFlag & friends (stdlib enums)
│   └── exceptions.py
├── persistence/
│   └── state.py           # StateStore: stdlib sqlite3 (flags, lock keys, item state)
├── notification.py        # Notifier protocol + NullNotifier (tests, headless)
├── telegrambot.py         # TelegramNotifier: python-telegram-bot v21+, isolated
└── cli/
    └── supervisor.py      # chimera-supervisor: validate / migrate / control
```

## Configuration

### Where it lives

The old system loaded YAML **into a SQLite DB** with `chimera-supervisor --new/--update`;
the engine then read the DB. Editing a config required a reload dance and the DB
mixed definitions with runtime state (`lastUpdate`, `status`, reference times).

New system: the Supervisor `__config__` gets `checklist_dir` (default
`~/.chimera/supervisor`). At `__start__` (and on `reload`, also exposed via
Telegram `/reload` and the CLI), every `*.yaml` in that directory is parsed and
validated. The files are the single source of truth; nothing about definitions
is ever written anywhere else. Malformed files are reported and skipped as a
whole (never half-loaded).

### New format

Top-level mapping `checklist:` of item-name → item (matches the `example.yaml`
brainstorm; names must be unique across all loaded files):

```yaml
checklist:
  open_dome_at_sunset:
    description: Open dome at sunset and start fans
    active: true                # default true
    run: on_change              # on_change (default) | always   [was: eager]
    on_error: continue          # continue (default) | abort     [was: eager_response]
    conditions:                 # ALL must hold (AND); OR = write a second item
      - condition: time
        after: sunset           # sunset|sunrise, *_twilight_begin|*_twilight_end, "HH:MM" UT
        offset: -2h             # "30m", "-2h", "1h30m", or plain hours (float)
      - condition: dome
        slit: closed            # slit/flap: open|closed
      - condition: flag
        instrument: site
        is: ready               # is|is_not: unset|ready|operating|close|lock|error
      - condition: flag
        instrument: dome
        is_not: lock
    responses:
      - action: set_flag
        instrument: site
        flag: ready
      - action: telescope
        do: unpark              # park|unpark|open_cover|close_cover|stop_tracking|slew
      - action: dome
        do: open_slit           # open_slit|close_slit|open_flap|close_flap|track|stand|slew
      - action: dome
        do: slew
        azimuth: oppose_sun     # degrees or oppose_sun
      - action: fan
        do: switch_on           # switch_on|switch_off
        fan: /CSKFan/DomeFanEast
        speed: 600              # optional
      - action: lamp
        do: switch_off
        lamp: /SchneiderOTBLamp/building
      - action: send_photo
        url: http://camera.example/image.jpg
```

Full condition catalog (legacy `type`/`mode` → new form):

| legacy | new |
|---|---|
| `CheckTime mode=±1/0, deltaTime` | `condition: time, after/before: sunset, offset:` |
| `CheckTime mode=±2` | `after/before: sunset_twilight_begin` |
| `CheckTime mode=±3` | `after/before: sunset_twilight_end` |
| `CheckTime mode=±4` | `after/before: sunrise` |
| `CheckTime mode=±5` | `after/before: sunrise_twilight_begin` |
| `CheckTime mode=±6` | `after/before: sunrise_twilight_end` |
| `CheckTime mode>6, time` | `after/before: "HH:MM"` |
| `CheckDome 0/1/2/3` | `condition: dome, slit: open/closed, flap: open/closed` |
| `CheckTelescope ±1..±4` | `condition: telescope, state: [not_]parked/cover_open/cover_closed/[not_]slewing/[not_]tracking` |
| `CheckTelescope ±5` | `state: m1_warmer_than_front_ring / m1_cooler_than_front_ring` |
| `CheckWeatherStation mode 0/1, index` | `condition: weather_station, station: N, state: ok/stale` |
| `CheckHumidity 0 / 1` | `condition: humidity, above: X` / `below: X, for: 1h` |
| `CheckTemperature 0 / 1` | `condition: temperature, below: X` / `above: X, for: 1h` |
| `CheckWindSpeed 0 / 1` | `condition: wind_speed, above: X` / `below: X, for: 1h` |
| `CheckTransparency 0 / 1` | `condition: transparency, below: X` / `above: X, for: 1h` |
| `CheckDew 0 / 1 (tempdiff)` | `condition: dew_gap, below: X` / `above: X, for: 1h` (gap = T − T_dew) |
| `CheckDewPoint` | `condition: dew_point, below/above: X` |
| `CheckInstrumentFlag 0/1/2/3` | `condition: flag, is:/is_not:/locked_with_key:/not_locked_with_key:` |
| `AskListener question waittime` | `condition: ask_operator, question:, timeout: 120s` |

Action catalog:

| legacy | new |
|---|---|
| `DomeAction 0/1/2/3` | `action: dome, do: open_slit/close_slit/open_flap/close_flap` |
| `DomeAction 4 parameter` | `action: dome, do: slew, azimuth: 90 / oppose_sun` |
| `DomeAction 9/10` | `action: dome, do: track/stand` |
| `DomeAction 5/6 "fan[,speed]"`, `TelescopeAction 8/9` | `action: fan, do: switch_on/switch_off, fan:, speed:` |
| `DomeAction 7/8 parameter` | `action: lamp, do: switch_on/switch_off, lamp:` |
| `TelescopeAction 0/1/2/3/7` | `action: telescope, do: unpark/park/open_cover/close_cover/stop_tracking` |
| `TelescopeAction 5/6` | `action: telescope, do: slew, alt:/az:` or `ra:/dec:` |
| `SetInstrumentFlag flag=1,3…` | `action: set_flag, instrument:, flag: ready/close/…` (names, not ints) |
| `LockInstrument`/`UnlockInstrument` | `action: lock/unlock, instrument:, key:` |
| `SendTelegram message` | `action: notify, message:` |
| `SendPhoto path` | `action: send_photo, url:, message:` |
| `ExecuteScript filename` | `action: run_script, path:` |
| `ConfigureScheduler filename` | `action: configure_scheduler, file:` |
| `StartScheduler`/`StopScheduler` | `action: scheduler, do: start/stop` |
| `StartRobObs`/`StopRobObs` | `action: robobs, do: start/stop/wake` |
| `StopAll` | `action: stop_all` |
| `Question` | `action: ask_operator, question:, timeout:` |

Durations accept `"90s"`, `"30m"`, `"2h"`, `"1h30m"`, or bare numbers
(hours for `offset:`/`for:`, seconds for `timeout:` — matching legacy units).

### Legacy format & migration

`core/legacy.py` reads the old `checklist:`-list format (types + modes) and
converts each item into the new model — this is also how we prove equivalence.
`chimera-supervisor migrate <in> [-o out]` rewrites old files to the new format.
The test suite converts **all** T80-South production files and asserts each
legacy check/response maps to the intended condition/action with identical
parameters. The engine itself only knows the new model; legacy support is a
pure front-end conversion.

### Controller `__config__` (renamed keys)

```
site, telescope, camera, dome, scheduler, robobs, weatherstations   (locations; comma-list ok)
fans:                    (was implicit in DomeAction parameters; optional list for discovery)
checklist_dir:           NEW  directory of checklist YAMLs
state_db:                NEW  path of runtime-state sqlite (default ~/.chimera/supervisor_state.db)
telegram_token:          was telegram-token
telegram_broadcast_ids:  was telegram-broascast-ids (typo fixed)
telegram_listen_ids:     was telegram-listen-ids
freq:                    unchanged (Hz)
max_weather_age:         was max_mins (minutes)
```

## Repository split

The robotic-observation subsystem (RobObs controller, its scheduler DB and
planning algorithms, the `chimera-robobs` CLI) is **not part of this package
anymore**: it lives in its own repository, `chimera-robobs` (package
`chimera_robobs`), ported to Python 3 / chimera 0.2 there. The two do not
import each other. The supervisor still *supervises* a robobs controller —
the `robobs` config role points at its chimera location and the
`action: robobs` response drives it purely over proxy calls
(`start/stop/wake`), exactly as it drives the scheduler.

## SQLAlchemy trade-off

**Supervisor: drop SQLAlchemy entirely — use stdlib `sqlite3`.**

- With definitions moved to YAML, what remains is tiny runtime state:
  instrument flags, lock keys, per-item last status/timestamps, per-condition
  reference times ("below X *for 2h*"). Three small tables, key-value access
  patterns, no joins, no polymorphism. An ORM buys nothing here.
- The old code needed a full rewrite anyway: it used the pre-1.0 API
  (`relation`, import-time global engines, `metaData.bind`). There is no
  "keep sqlalchemy to save work" option.
- Import-time `create_engine`/`create_all` globals were a root cause of the
  old entanglement and untestability. `StateStore(path)` is injected,
  WAL-mode, thread-safe, trivially testable with `:memory:`/tmp paths.
- One fewer version constraint: chimera core pins `sqlalchemy==1.4`; not
  depending on it at all means supervisor core can't conflict.

- With robobs extracted to its own repository, chimera-supervisor has **no
  SQLAlchemy dependency at all** (the `configure_scheduler` action uses
  chimera's own scheduler model, which chimera itself provides).

**RobObs scheduling DB (now in the chimera-robobs repo): keep SQLAlchemy
(1.4, same pin as chimera core).**

- Genuinely relational: projects/targets/blocks/programs + a polymorphic
  action hierarchy that must round-trip into chimera's own scheduler model
  (which is itself SQLAlchemy). Hand-rolling that in sqlite3 would be more
  code and more bugs than the ORM.
- Reorganized schema (fixes the chimera-sched copy/paste): FKs point at real
  primary keys (no more `program.name → targets.name`), snake_case columns,
  no unused tables/columns, engine/session injected (no import-time side
  effects), `echo`/path configurable.

## Persistence schema (supervisor state, sqlite3)

```
instrument_flags(instrument TEXT PK, flag TEXT, last_update TEXT, last_change TEXT)
lock_keys(instrument TEXT, key TEXT, active INTEGER, updated TEXT, PRIMARY KEY(instrument, key))
item_state(name TEXT PK, last_status INTEGER, last_update TEXT, last_change TEXT)
condition_state(item TEXT, index INTEGER, since TEXT, PRIMARY KEY(item, index))
```

Flags stored as names (`"ready"`), not enum ints — survives enum reordering.

## Engine semantics (unchanged, now documented)

- Conditions are evaluated in order; first failure stops the item (short-circuit AND).
- Responses fire when all conditions pass AND (item is `run: always` or the
  aggregate status flipped false→true since the last evaluation).
- `on_error: continue` runs remaining responses even if one fails ("close
  everything you can"); `abort` stops at the first failure ("don't take flats
  if the dome didn't open").
- `ask_operator` is a *condition* (blocks up to `timeout`, defaults to "No").
- Instrument locking: an instrument may hold several named keys; every key
  must be released before the flag leaves `lock`. Same behavior as before,
  now in `StateStore` with tests.

## Operator interfaces (Telegram now, Slack-ready)

Chat integrations are split into a transport layer and a shared command layer
so a Slack (or Discord, Matrix, …) bot can be added without touching
supervisor logic:

- `core/context.py::Notifier` — the outbound protocol the checklist uses:
  `broadcast`, `broadcast_photo`, `ask`. The controller and engine only ever
  see this; tests use `NullNotifier`/`RecordingNotifier`.
- `operator.py::SupervisorPort` — the narrow inbound API a bot may call
  (`manual_items`, `run_action`, `status_summary`, `lock_instrument`,
  `unlock_instrument`, `reload_checklist`).
- `operator.py::OperatorCommands` — transport-agnostic dispatch of `/list`,
  `/run`, `/info`, `/lock`, `/unlock`, `/reload`, `/help`. Returns a `Reply`
  (text + optional buttons) that the transport renders natively (Telegram
  inline keyboards, Slack Block Kit buttons, …).
- `telegrambot.py::TelegramNotifier` — the only Telegram-specific file
  (python-telegram-bot ≥ 21, asyncio in a daemon thread). A future
  `slackbot.py` implements the same two protocols (`Notifier` + command
  transport delegating to `OperatorCommands`) and plugs into the controller's
  `_setup_notifier()` unchanged.

Commands are only accepted from the configured chat ids (the legacy bot
accepted `/run` from anyone). No telegram import happens unless a token is
configured.

## RobObs port (in the chimera-robobs repository)

Mechanical-but-careful port: Python 3 syntax, snake_case chimera API, injected
session factory, restored missing imports in `algorithms/base.py` (the legacy
tree is broken at runtime — every algorithm call `NameError`s), and the known
bugs fixed (wrong-DB backups in the CLI, uncommitted `reset_scheduler` session,
`RecurrentDB.blockid` tuple assignment, `Projects.__str__`). Behavior is
otherwise preserved; it needs on-sky validation before production use. See
`chimera-robobs/docs/robobs-port-notes.md`.

## Testing

- `pytest`; no hardware, no network, no bus needed for the core: conditions
  and actions take plain proxies/values, so tests use lightweight fakes.
- Config: every t80s production file parses (legacy), migrates, re-parses
  (new), and the resulting item trees are semantically compared field by field.
  Real files are read from `supervisor/` (untracked copy in this repo) or
  `T80S_SUPERVISOR_DIR`; sanitized fixtures cover CI.
- StateStore: full coverage including lock-key edge cases (the multi-key
  unlock rules).
- Engine: status-change vs always, on_error modes, abort, persistence of
  `for:` reference times across restarts.
