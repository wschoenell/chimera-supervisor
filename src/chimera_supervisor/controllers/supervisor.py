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

#: config role -> the Context attribute it populates (roles are singular in
#: the config, plural lists on the context)
_ROLE_CONTEXT_ATTR = {
    "site": "site",
    "telescope": "telescopes",
    "camera": "cameras",
    "dome": "domes",
    "scheduler": "schedulers",
    "robobs": "robobs",
    "weatherstations": "weather_stations",
}

#: roles that get one flag PER instance (``weatherstations_01``, …). Every
#: other role is addressed by its bare name from actions and conditions.
_PLURAL_FLAG_ROLES = ("weatherstations",)

#: roles whose flag is VOLATILE: their controllers (the chimera Scheduler,
#: RobObs) come back OFF after a chimera restart - they never auto-resume -
#: so any value persisted in state.db is stale. Left at "operating"/"error"
#: they deadlock the night: open_dome_at_sunset holds while the scheduler is
#: operating and start_robobs holds while robobs is operating (2026-07-26,
#: and hand-cleared ~10 times over earlier nights). Reset to READY at boot.
_VOLATILE_ROLES = ("scheduler", "robobs")


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
        "telegram_broadcast_ids": None,  # chat ids: YAML list or comma-separated
        "telegram_listen_ids": None,  # chat ids allowed to answer questions
        # verify TLS when send_photo fetches an https webcam/all-sky image;
        # set False for observatories whose feeds use self-signed certs
        # (private-network hosts are trusted regardless)
        "photo_verify_ssl": True,
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
        # RLock, not Lock: run_action() takes it, and a checklist response
        # may itself call back into run_action through ctx.run_action while
        # the cycle already holds it (same thread -> must be reentrant).
        self._cycle_lock = threading.RLock()
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

        # reconcile the volatile flags with reality: a restarted scheduler /
        # robobs is always OFF, so clear any stale operating/error persisted
        # across the restart (issue #15)
        self._reset_volatile_flags()

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
            # Rebuild the context HERE, for the same reason the events are
            # connected here: during __start__ the bus is not serving yet, so
            # get_proxy() fails and every role silently came up empty - a
            # supervisor that looks healthy while supervising nothing.
            with self._cycle_lock:
                self.engine.ctx = self._build_context()
            missing = self._verify_roles()
            if missing:
                message = (
                    "supervisor role(s) configured but NOT resolved: "
                    + ", ".join(missing)
                    + " — checklist items guarding them cannot fire"
                )
                self.log.error(message)
                try:
                    self.notifier.broadcast(message)
                except Exception:
                    self.log.exception("could not broadcast missing-role alert")
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
            if not raw:
                return []
            parts = raw if isinstance(raw, list | tuple) else str(raw).split(",")
            return [int(str(part).strip()) for part in parts if str(part).strip()]

        self.notifier = TelegramNotifier(
            token=str(token),
            broadcast_ids=_ids("telegram_broadcast_ids"),
            listen_ids=_ids("telegram_listen_ids"),
            supervisor=self,
            log=self.log,
            verify_ssl=bool(self["photo_verify_ssl"]),
        )
        self.notifier.start()

    def _flag_names(self, role: str) -> list[str]:
        """Flag-board names for a role: bare name for a single instance,
        ``role_01``… for several (the naming the configs rely on).

        Only ``weatherstations`` pluralises.  Every flag-aware action and
        condition addresses the other roles by their bare name
        (``DomeAction`` writes ``"dome"``, ``TelescopeAction`` writes
        ``"telescope"``), so pluralising them registered ``dome_01``/
        ``dome_02`` flags that nothing ever read or wrote while the actions
        auto-created an unregistered ``"dome"`` entry behind their backs.
        """
        locations = self._locations.get(role, [])
        if role not in _PLURAL_FLAG_ROLES:
            if len(locations) > 1:
                self.log.warning(
                    "role %r has %d locations but a single flag board entry "
                    "(%r): they share one flag",
                    role,
                    len(locations),
                    role,
                )
            return [role]
        if len(locations) <= 1:
            return [role]
        return [f"{role}_{i + 1:02d}" for i in range(len(locations))]

    def _reset_volatile_flags(self) -> None:
        """Clear stale scheduler/robobs flags a restart persisted (issue #15).

        Only touches CONFIGURED roles (a registered flag) and only the
        volatile ones, so weather/dome/site persistence is untouched; a
        LOCK is an operator decision and is never overridden.
        """
        registered = set(self.store.instruments())
        for role in _VOLATILE_ROLES:
            for name in self._flag_names(role):
                if name not in registered:
                    continue
                if self.store.get_flag(name) == Flag.LOCK:
                    continue
                self.store.set_flag(name, Flag.READY)

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
                # ERROR, not WARNING: a configured role that resolves to
                # nothing leaves every guard over it reading False, which
                # disables close-down items instead of firing them.
                self.log.error("could not get proxy for %s (%s)", role, location)
        return proxies

    def _verify_roles(self) -> list[str]:
        """Names of configured roles that resolved to no proxy at all."""
        missing = []
        for role in _ROLES:
            if not self._locations.get(role):
                continue  # not configured for this site: legitimately absent
            attribute = _ROLE_CONTEXT_ATTR[role]
            if not getattr(self.engine.ctx, attribute, None):
                missing.append(role)
        return missing

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
            log=self.log,
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
        if not items:
            # an empty checklist is a running supervisor that supervises
            # nothing - never let that pass as a routine info line
            message = f"checklist is EMPTY: no items found in {directory}"
            self.log.warning(message)
            try:
                self.notifier.broadcast(message)
            except Exception:
                self.log.exception("could not broadcast empty-checklist warning")
            return message
        self.log.info(message)
        return message

    #: how long an operator command waits for the current checklist cycle
    #: before answering "busy" (a cycle can legitimately take proxy_timeout)
    _OPERATOR_LOCK_WAIT = 30.0

    def run_action(self, name: str) -> bool:
        """Run an item's responses immediately (skips its conditions).

        Serialised against the checklist cycle: this is reachable from the
        Telegram thread, from _run_hook threads and from the CLI over the
        bus, none of which held _cycle_lock, so `/run park_telescope` could
        drive the same telescope and dome proxies as a concurrent
        open_dome_at_sunset — or run while reload_checklist was swapping
        engine.items and engine.ctx underneath it.

        The wait is BOUNDED: an operator command that cannot get in says so
        instead of hanging the bot behind a slow cycle.
        """
        if not self._cycle_lock.acquire(timeout=self._OPERATOR_LOCK_WAIT):
            self.log.warning(
                "run_action(%r): checklist cycle still busy after %.0f s; refusing",
                name,
                self._OPERATOR_LOCK_WAIT,
            )
            return False
        try:
            return self.engine.run_action(name)
        finally:
            self._cycle_lock.release()

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
        # status OK is deliberately NOT broadcast: the scheduler stops
        # tracking at the end of every program, so it fires many times a
        # night and was pure noise on telegram.
        self._set_flag_safe("telescope", Flag.READY)
        if status == TelescopeStatus.OK:
            return

        # Anything else IS abnormal and must reach the operator on its own,
        # not only through an optional checklist hook: routing it solely to
        # ON_OBJECT_TOO_LOW meant that on a site which never defined that
        # item (every lna40 checklist) an abnormal stop surfaced nowhere but
        # chimera.log.
        self.log.warning("telescope tracking stopped abnormally: %s", status)
        try:
            self.notifier.broadcast(f"Telescope tracking stopped: {status}")
        except Exception:
            self.log.exception("could not broadcast abnormal tracking stop")
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
