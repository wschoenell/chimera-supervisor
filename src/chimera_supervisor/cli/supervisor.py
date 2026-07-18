# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""chimera-supervisor command line tool.

Offline commands (no running chimera needed):

    chimera-supervisor validate <file-or-dir>...
    chimera-supervisor migrate  <legacy.yaml>... [-o OUTDIR]
    chimera-supervisor example

Online commands (talk to a running Supervisor controller):

    chimera-supervisor info | items | run <item> | reload | wakeup
    chimera-supervisor lock <instrument> <key> | unlock <instrument> <key>
    chimera-supervisor start | stop
"""

import argparse
import pathlib
import random
import sys

import yaml

from chimera_supervisor.core import checklist, legacy
from chimera_supervisor.core.exceptions import ConfigError

EXAMPLE = """\
# chimera-supervisor checklist example (new format).
# Conditions are ANDed; for OR write two items. Items without conditions are
# manual procedures ("/run" or CLI run). See docs/configuration.rst.
checklist:
  close_on_dew:
    description: Close everything when close to condensation
    conditions:
      - condition: dome
        slit: open
      - condition: dew_gap        # ambient temperature minus dew point
        below: 4                  # degrees Celsius
    responses:
      - action: stop_all
      - action: telescope
        do: close_cover
      - action: dome
        do: close_slit
      - action: lock              # keep it closed until the lock is released
        instrument: dome
        key: dew

  reopen_after_dew:
    conditions:
      - condition: flag
        instrument: dome
        locked_with_key: dew
      - condition: dew_gap
        above: 4
        for: 1h                   # must hold for 1 hour
    responses:
      - action: unlock
        instrument: dome
        key: dew

  open_dome_at_sunset:
    description: Open dome at sunset and start the fans
    run: always                   # fire every cycle while conditions hold
    on_error: abort               # stop at the first failed response
    conditions:
      - condition: time
        after: sunset
        offset: -2h
      - condition: dome
        slit: closed
      - condition: flag
        instrument: site
        is: ready
      - condition: flag
        instrument: dome
        is_not: lock
    responses:
      - action: telescope
        do: unpark
      - action: dome
        do: open_slit
      - action: fan
        do: switch_on
        fan: /FakeFan/fake
        speed: 600
      - action: dome
        do: slew
        azimuth: oppose_sun

  park_telescope:                 # manual procedure (no conditions)
    responses:
      - action: stop_all
      - action: telescope
        do: close_cover
      - action: dome
        do: close_slit
      - action: telescope
        do: park
      - action: notify
        message: Telescope parked.
"""


def cmd_validate(args) -> int:
    """Validate checklist files (new format; legacy files are validated
    through the converter and reported as needing migration)."""
    failures = 0
    paths: list[pathlib.Path] = []
    for target in args.paths:
        path = pathlib.Path(target)
        if path.is_dir():
            paths.extend(
                p for p in sorted(path.glob("*.yaml")) if not p.name.startswith(".")
            )
        else:
            paths.append(path)

    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as e:
            print(f"FAIL  {path}: {e}")
            failures += 1
            continue
        try:
            if checklist.is_legacy_document(doc):
                items, _, warnings = legacy.convert_file(path)
                note = f"legacy format, {len(items)} item(s) — convert it with 'migrate'"
                for warning in warnings:
                    print(f"      warning: {warning}")
            else:
                items = checklist.parse_document(doc, str(path))
                note = f"{len(items)} item(s)"
            print(f"OK    {path}: {note}")
        except ConfigError as e:
            print(f"FAIL  {path}: {e}")
            failures += 1

    if failures:
        print(f"\n{failures} file(s) failed validation")
    return 1 if failures else 0


def cmd_migrate(args) -> int:
    """Convert legacy checklist files to the new format."""
    status = 0
    outdir = pathlib.Path(args.output) if args.output else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    for target in args.files:
        path = pathlib.Path(target)
        try:
            items, converted, warnings = legacy.convert_file(path)
        except ConfigError as e:
            print(f"FAIL  {path}: {e}", file=sys.stderr)
            status = 1
            continue
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        text = checklist.dump_items(items)
        if outdir:
            destination = outdir / path.name
            destination.write_text(text)
            print(f"OK    {path} -> {destination} ({len(items)} item(s))", file=sys.stderr)
        else:
            print(f"# migrated from {path}")
            print(text)
    return status


def cmd_example(args) -> int:
    print(EXAMPLE, end="")
    return 0


# ----------------------------------------------------------------------
# online commands
# ----------------------------------------------------------------------


def _proxy(args):
    import threading
    import time

    from chimera.core.bus import Bus
    from chimera.core.proxy import Proxy

    bus = Bus(f"tcp://{args.host}:{random.randint(10000, 60000)}")
    # the client bus must run its receive loop, or replies never arrive
    threading.Thread(target=bus.run_forever, daemon=True).start()
    started = getattr(bus, "_bus_started", None)
    if started is not None:
        started.wait(5)
    else:
        time.sleep(0.5)
    url = f"tcp://{args.host}:{args.port}{args.supervisor}"
    proxy = Proxy(url, bus)
    proxy.resolve()
    return bus, proxy


def _online(args, call) -> int:
    bus = None
    try:
        bus, proxy = _proxy(args)
        return call(proxy) or 0
    except Exception as e:
        print(f"error: could not talk to the supervisor at "
              f"{args.host}:{args.port}{args.supervisor}: {e}", file=sys.stderr)
        return 1
    finally:
        if bus is not None:
            bus.shutdown()


def cmd_info(args) -> int:
    return _online(args, lambda proxy: print(proxy.status_summary()))


def cmd_items(args) -> int:
    def call(proxy):
        manual = set(proxy.manual_items())
        for name in proxy.items():
            print(f"{name}{'  [manual]' if name in manual else ''}")

    return _online(args, call)


def cmd_run(args) -> int:
    def call(proxy):
        ok = proxy.run_action(args.item)
        print(f"{args.item}: {'done' if ok else 'FAILED'}")
        return 0 if ok else 1

    return _online(args, call)


def cmd_reload(args) -> int:
    return _online(args, lambda proxy: print(proxy.reload_checklist()))


def cmd_lock(args) -> int:
    return _online(args, lambda proxy: proxy.lock_instrument(args.instrument, args.key))


def cmd_unlock(args) -> int:
    def call(proxy):
        released = proxy.unlock_instrument(args.instrument, args.key)
        print("unlocked" if released else "was not locked with that key")

    return _online(args, call)


def cmd_start(args) -> int:
    return _online(args, lambda proxy: proxy.start_checklist() and None)


def cmd_stop(args) -> int:
    return _online(args, lambda proxy: proxy.stop_checklist() and None)


def cmd_wakeup(args) -> int:
    return _online(args, lambda proxy: proxy.wakeup() and None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chimera-supervisor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="chimera server host")
    parser.add_argument("--port", type=int, default=6379, help="chimera server port")
    parser.add_argument(
        "--supervisor", default="/Supervisor/0", help="supervisor controller location"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate checklist files or directories")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("migrate", help="convert legacy files to the new format")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output", help="directory for converted files (default: stdout)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("example", help="print an example checklist file")
    p.set_defaults(func=cmd_example)

    for name, func, doc in (
        ("info", cmd_info, "instrument flags and lock keys"),
        ("items", cmd_items, "list loaded checklist items"),
        ("reload", cmd_reload, "reload checklist files"),
        ("start", cmd_start, "resume automatic checklist runs"),
        ("stop", cmd_stop, "pause automatic checklist runs"),
        ("wakeup", cmd_wakeup, "run a checklist cycle now"),
    ):
        p = sub.add_parser(name, help=doc)
        p.set_defaults(func=func)

    p = sub.add_parser("run", help="run an item's responses now")
    p.add_argument("item")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("lock", help="lock an instrument with a key")
    p.add_argument("instrument")
    p.add_argument("key")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("unlock", help="release a lock key")
    p.add_argument("instrument")
    p.add_argument("key")
    p.set_defaults(func=cmd_unlock)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
