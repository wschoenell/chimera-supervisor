# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""The Supervisor chimera controller.

Thin integration layer: owns the checklist engine, the state store and the
notifier, translates chimera events into flag changes, and exposes the
operator API (used by the CLI and the Telegram bot).  All checklist logic
lives in :mod:`chimera_supervisor.core`.
"""

import datetime
import logging
import logging.handlers
import os
import threading

from chimera.controllers.scheduler.states import State as SchedState
from chimera.controllers.scheduler.status import SchedulerStatus
from chimera.core.chimeraobject import ChimeraObject
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.core.event import event
from chimera.interfaces.telescope import TelescopeStatus

from chimera_supervisor.core import checklist
from chimera_supervisor.core.context import Context
from chimera_supervisor.core.engine import Engine, Observer
from chimera_supervisor.core.exceptions import ConfigError
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag
from chimera_supervisor.notification import NullNotifier
from chimera_supervisor.persistence.state import StateStore

#: checklist item names run in reaction to chimera events, when defined
ON_SCHEDULER_ERROR = "on_scheduler_error"
ON_OBJECT_TOO_LOW = "on_object_too_low"

_ROLES = (
    "site",
    "telescope",
    "camera",
    "dome",
    "scheduler",
    "robobs",
    "weatherstations",
)


class Supervisor(ChimeraObject):
    __config__ = {
        "site": "/Site/0",
        "telescope": "/Telescope/0",
        "camera": "/Camera/0",
        "dome": "/Dome/0",
        "scheduler": None,  # comma-separated locations allowed
        "robobs": None,
        "weatherstations": None,
        # directory with checklist YAML files (new format)
        "checklist_dir": os.path.join(SYSTEM_CONFIG_DIRECTORY, "supervisor"),
        # runtime state (flags, lock keys, item status)
        "state_db": os.path.join(SYSTEM_CONFIG_DIRECTORY, "supervisor_state.db"),
        "telegram_token": None,
        "telegram_broadcast_ids": None,  # comma-separated chat ids
        "telegram_listen_ids": None,  # chat ids allowed to answer questions
        "freq": 0.01,  # checklist frequency (Hz)
        "max_weather_age": 10.0,  # minutes before weather data is stale
        # bound on every proxied instrument call (seconds): a hung
        # instrument fails the action instead of freezing the engine
        "proxy_timeout": 300.0,
    }

    def __init__(self):
        super().__init__()
        self.store: StateStore | None = None
        self.engine: Engine | None = None
        self.notifier = NullNotifier()
        self._locations: dict[str, list[str]] = {}
        self._running = True
        self._shutdown = threading.Event()
        self._trigger = threading.Event()
        self._cycle_lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def __start__(self):
        # debugging aid: kill -USR1 <pid> dumps all thread stacks to stderr
        import faulthandler
        import signal

        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except (ValueError, RuntimeError):
            pass  # not on the main thread / unsupported platform

        self._setup_logger()
        self.store = StateStore(self["state_db"])

        for role in _ROLES:
            if self[role] is None:
                continue
            locations = [
                loc.strip() for loc in str(self[role]).split(",") if loc.strip()
            ]
            self._locations[role] = locations
            for name in self._flag_names(role):
                self.store.register_instrument(name)

        self._setup_notifier()

        self.engine = Engine(
            ctx=self._build_context(),
            store=self.store,
            observer=Observer(
                check_begin=lambda item, cond: self.check_begin(item.name, cond.kind),
                check_complete=lambda item, cond, res: self.check_complete(
                    item.name, cond.kind, res.passed, res.message
                ),
                item_status_changed=lambda item, status: self.item_status_changed(
                    item.name, status
                ),
                response_begin=lambda item, resp: self.item_response_begin(
                    item.name, resp.kind
                ),
                response_complete=lambda item, resp, ok: self.item_response_complete(
                    item.name, resp.kind, ok
                ),
            ),
            log=self.log,
        )
        self.reload_checklist()

        # event subscription happens on the first control() tick: during
        # __start__ the bus is not serving yet, so proxies can't resolve
        self._events_connected = False

        self._worker = threading.Thread(
            target=self._work_loop, name="supervisor-engine", daemon=True
        )
        self._worker.start()

        self.set_hz(self["freq"])

    def __stop__(self):
        self._shutdown.set()
        if self.engine is not None:
            self.engine.abort()
        self._trigger.set()
        if self._worker is not None:
            self._worker.join(timeout=5)
        if hasattr(self.notifier, "stop"):
            try:
                self.notifier.stop()
            except Exception:
                self.log.exception("error stopping notifier")
        if self.store is not None:
            self.store.close()

    def control(self):
        """Called by chimera at ``freq`` Hz: schedule one checklist cycle."""
        if not self._events_connected:
            self.log.info("control loop alive; subscribing to instrument events")
            self._connect_telescope_events()
            self._connect_scheduler_events()
            self._events_connected = True
            self.log.info("event subscription done; scheduling first checklist cycle")
        self.log.debug("control tick: triggering checklist cycle")
        self._trigger.set()
        return True

    def _work_loop(self):
        while not self._shutdown.is_set():
            if not self._trigger.wait(timeout=1.0):
                continue
            self._trigger.clear()
            if not self._running or self._shutdown.is_set():
                continue
            with self._cycle_lock:
                try:
                    self.engine.run_cycle()
                except Exception:
                    self.log.exception("checklist cycle failed")

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------

    def _setup_logger(self):
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(SYSTEM_CONFIG_DIRECTORY, "supervisor.log"),
            maxBytes=50 * 1024 * 1024,
            backupCount=10,
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s %(threadName)s] %(message)s")
        )
        handler.setLevel(logging.DEBUG)
        self.log.addHandler(handler)

    def _setup_notifier(self):
        token = self["telegram_token"]
        if token is None:
            self.log.info(
                "no telegram_token configured; notifications go to the log only"
            )
            self.notifier = NullNotifier(self.log)
            return
        from chimera_supervisor.telegrambot import TelegramNotifier

        def _ids(key):
            raw = self[key]
            return (
                [int(part) for part in str(raw).split(",") if part.strip()]
                if raw
                else []
            )

        self.notifier = TelegramNotifier(
            token=str(token),
            broadcast_ids=_ids("telegram_broadcast_ids"),
            listen_ids=_ids("telegram_listen_ids"),
            supervisor=self,
            log=self.log,
        )
        self.notifier.start()

    def _flag_names(self, role: str) -> list[str]:
        """Flag-board names for a role: bare name for a single instance,
        ``role_01``… for several (the naming the configs rely on)."""
        locations = self._locations.get(role, [])
        if len(locations) <= 1:
            return [role]
        return [f"{role}_{i + 1:02d}" for i in range(len(locations))]

    def _proxies(self, role: str) -> list:
        proxies = []
        for location in self._locations.get(role, []):
            try:
                proxy = self.get_proxy(location)
                # older cores have no per-proxy timeout: degrade to unbounded
                if hasattr(proxy, "__timeout__"):
                    proxy.__timeout__ = float(self["proxy_timeout"])
                proxies.append(proxy)
            except Exception:
                self.log.warning("could not get proxy for %s (%s)", role, location)
        return proxies

    def _build_context(self) -> Context:
        return Context(
            site=(self._proxies("site") or [None])[0],
            telescopes=self._proxies("telescope"),
            domes=self._proxies("dome"),
            cameras=self._proxies("camera"),
            weather_stations=self._proxies("weatherstations"),
            schedulers=self._proxies("scheduler"),
            robobs=self._proxies("robobs"),
            flags=self.store,
            notifier=self.notifier,
            max_weather_age=datetime.timedelta(minutes=float(self["max_weather_age"])),
            resolve=self.get_proxy,
            run_action=self.run_action,
        )

    # ------------------------------------------------------------------
    # operator API (CLI and Telegram bot)
    # ------------------------------------------------------------------

    def reload_checklist(self) -> str:
        """(Re)load every checklist YAML from checklist_dir."""
        directory = os.path.expanduser(str(self["checklist_dir"]))
        try:
            items = checklist.load_directory(directory)
        except ConfigError as e:
            message = f"checklist reload FAILED, keeping previous configuration: {e}"
            self.log.error(message)
            return message
        with self._cycle_lock:
            self.engine.ctx = self._build_context()
            self.engine.load(items)
        message = (
            f"checklist loaded: {len(items)} item(s) from {directory} "
            f"({len(self.engine.manual_items())} manual)"
        )
        self.log.info(message)
        return message

    def run_action(self, name: str) -> bool:
        """Run an item's responses immediately (skips its conditions)."""
        return self.engine.run_action(name)

    def items(self) -> list[str]:
        return [item.name for item in self.engine.items]

    def manual_items(self) -> list[str]:
        return self.engine.manual_items()

    def activate(self, name: str) -> bool:
        item = self.engine.item(name)
        if item is None:
            return False
        item.active = True
        return True

    def deactivate(self, name: str) -> bool:
        item = self.engine.item(name)
        if item is None:
            return False
        item.active = False
        return True

    def start_checklist(self) -> bool:
        self._running = True
        return True

    def stop_checklist(self) -> bool:
        self._running = False
        self.engine.abort()
        return True

    def wakeup(self) -> bool:
        """Trigger a checklist cycle right now."""
        self._trigger.set()
        return True

    def get_flag(self, instrument: str) -> str:
        return self.store.get_flag(instrument).value

    def set_flag(self, instrument: str, flag: str) -> None:
        self.store.set_flag(instrument, Flag.parse(flag))

    def lock_instrument(self, instrument: str, key: str) -> None:
        self.store.lock(instrument, key)

    def unlock_instrument(self, instrument: str, key: str) -> bool:
        return self.store.unlock(instrument, key)

    def status_summary(self) -> str:
        lines = ["Instrument flags:"]
        for instrument in self.store.instruments():
            flag = self.store.get_flag(instrument)
            keys = self.store.active_keys(instrument) if flag == Flag.LOCK else []
            suffix = f" (keys: {', '.join(keys)})" if keys else ""
            lines.append(f"- {instrument}: {flag}{suffix}")
        return "\n".join(lines)

    def broadcast(self, message: str) -> None:
        self.notifier.broadcast(message)

    # ------------------------------------------------------------------
    # chimera events in
    # ------------------------------------------------------------------

    def _connect_telescope_events(self):
        telescopes = self._proxies("telescope")
        if not telescopes:
            self.log.warning("no telescope to watch")
            return
        tel = telescopes[0]
        me = self.get_proxy()
        try:
            tel.slew_begin += me._watch_slew_begin
            tel.tracking_stopped += me._watch_tracking_stopped
            tel.park_complete += me._watch_park_complete
            tel.unpark_complete += me._watch_unpark_complete
        except Exception as e:
            self.log.warning("could not subscribe to telescope events: %s", e)

    def _connect_scheduler_events(self):
        schedulers = self._proxies("scheduler")
        if not schedulers:
            return
        sched = schedulers[0]
        me = self.get_proxy()
        try:
            sched.program_begin += me._watch_program_begin
            sched.program_complete += me._watch_program_complete
            sched.state_changed += me._watch_state_changed
        except Exception as e:
            self.log.warning("could not subscribe to scheduler events: %s", e)

    def _set_flag_safe(self, instrument: str, flag: Flag):
        try:
            self.store.set_flag(instrument, flag)
        except Exception as e:
            self.log.warning("could not set %s flag to %s: %s", instrument, flag, e)

    def _watch_slew_begin(self, ra, dec, epoch):
        self._set_flag_safe("telescope", Flag.OPERATING)

    def _run_hook(self, name: str):
        """Run an event-hook item on its own thread.  Event watchers execute
        on the bus dispatch pool; running responses (which issue further bus
        requests) inline there can exhaust the pool and deadlock the bus."""
        threading.Thread(
            target=self.run_action, args=(name,), name=f"hook-{name}", daemon=True
        ).start()

    def _watch_tracking_stopped(self, status):
        # deliberately not broadcast: the scheduler stops tracking at the end of
        # every program, so this fires many times a night with status OK and is
        # noise on telegram. Abnormal stops are still handled by the hook below.
        self._set_flag_safe("telescope", Flag.READY)
        if status == TelescopeStatus.OBJECT_TOO_LOW and self.engine.item(
            ON_OBJECT_TOO_LOW
        ):
            # site policy hook: define an item with this name to react
            self._run_hook(ON_OBJECT_TOO_LOW)

    def _watch_park_complete(self):
        self.notifier.broadcast("Telescope parked.")
        self._set_flag_safe("telescope", Flag.CLOSE)
        self._set_flag_safe("dome", Flag.CLOSE)

    def _watch_unpark_complete(self):
        self.notifier.broadcast("Telescope unparked.")
        self._set_flag_safe("telescope", Flag.READY)
        self._set_flag_safe("dome", Flag.READY)

    def _watch_program_begin(self, program):
        if self.store.get_flag("scheduler") != Flag.OPERATING:
            self._set_flag_safe("scheduler", Flag.OPERATING)

    def _watch_program_complete(self, program, status, message=None):
        if status == SchedulerStatus.ERROR:
            text = "Scheduler in ERROR" + (f": {message}" if message else "")
            self.notifier.broadcast(text)
            self._set_flag_safe("scheduler", Flag.ERROR)
            if self.engine.item(ON_SCHEDULER_ERROR):
                self._run_hook(ON_SCHEDULER_ERROR)
        elif status == SchedulerStatus.ABORTED:
            self._set_flag_safe("scheduler", Flag.READY)
            if message:
                self.notifier.broadcast(str(message))

    def _watch_state_changed(self, new_state, old_state):
        if new_state == SchedState.BUSY:
            self._set_flag_safe("scheduler", Flag.OPERATING)
        else:
            self._set_flag_safe("scheduler", Flag.READY)

    # ------------------------------------------------------------------
    # chimera events out
    # ------------------------------------------------------------------

    @event
    def check_begin(self, item, condition):
        pass

    @event
    def check_complete(self, item, condition, passed, message):
        pass

    @event
    def item_status_changed(self, item, status):
        pass

    @event
    def item_response_begin(self, item, response):
        pass

    @event
    def item_response_complete(self, item, response, ok):
        pass
