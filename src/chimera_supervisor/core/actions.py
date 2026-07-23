# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Checklist action (response) catalog.

Actions are executed when all conditions of an item pass.  Like conditions
they are dataclasses parsed from mappings::

    - action: dome
      do: open_slit

``execute(ctx)`` performs the action through the context, broadcasting
operator-relevant progress via the notifier, and raises
:class:`~chimera_supervisor.core.exceptions.ActionError` on failure so the
engine can apply the item's ``on_error`` policy.
"""

import abc
import datetime
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, ClassVar

from chimera_supervisor.core.context import Context
from chimera_supervisor.core.durations import format_duration, parse_duration
from chimera_supervisor.core.exceptions import (
    ActionError,
    ConfigError,
    StatusUpdateError,
)
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag
from chimera_supervisor.core.parsing import (
    as_bool,
    as_choice,
    as_float,
    as_str,
    check_keys,
)

_REGISTRY: dict[str, type["Action"]] = {}


def action_kinds() -> list[str]:
    return sorted(_REGISTRY)


def parse_action(cfg: object, source: str) -> "Action":
    """Build an action from one entry of a ``responses:`` list."""
    if not isinstance(cfg, dict):
        raise ConfigError(
            f"response entry must be a mapping, got {cfg!r}", source=source
        )
    if "action" not in cfg:
        raise ConfigError(
            f"response entry missing 'action:' key: {cfg!r}", source=source
        )
    kind = str(cfg["action"]).strip().lower()
    try:
        cls = _REGISTRY[kind]
    except KeyError:
        raise ConfigError(
            f"unknown action {kind!r}; available: {action_kinds()}", source=source
        ) from None
    return cls._from_config(cfg, source)


class Action(abc.ABC):
    kind: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if kind:
            _REGISTRY[kind] = cls

    @classmethod
    @abc.abstractmethod
    def _from_config(cls, cfg: dict, source: str) -> "Action": ...

    @abc.abstractmethod
    def to_config(self) -> dict: ...

    @abc.abstractmethod
    def execute(self, ctx: Context) -> None: ...


def _broadcast(ctx: Context, message: str) -> None:
    if ctx.notifier is not None:
        ctx.notifier.broadcast(message)


# --------------------------------------------------------------------------
# dome
# --------------------------------------------------------------------------


def _guarded_open(ctx: Context, instrument: str, is_done, do_open, what: str) -> None:
    """Open something only if the flag board allows it, marking the
    instrument OPERATING first (legacy openFunc semantics)."""
    if not ctx.flags.can_open(instrument):
        _broadcast(ctx, f"Cannot {what} due to supervisor constraints.")
        raise ActionError(f"cannot {what}: supervisor constraints")
    try:
        ctx.flags.set_flag(instrument, Flag.OPERATING)
    except StatusUpdateError as e:
        _broadcast(ctx, str(e))
        raise
    except Exception:
        try:
            ctx.flags.set_flag(instrument, Flag.ERROR)
        finally:
            raise
    if not is_done():
        do_open()


def _guarded_close(ctx: Context, instrument: str, is_open, do_close, what: str) -> None:
    """Close something regardless of flag bookkeeping problems (legacy
    closeFunc semantics: closing must always be attempted)."""
    try:
        if ctx.flags.get_flag(instrument) == Flag.OPERATING:
            ctx.flags.set_flag(instrument, Flag.READY)
    except Exception as e:
        _broadcast(ctx, str(e))
    try:
        if is_open():
            do_close()
    except Exception as e:
        _broadcast(ctx, f"Could not {what}! ({e})")
        try:
            ctx.flags.set_flag(instrument, Flag.ERROR)
        except Exception:
            pass
        raise ActionError(f"could not {what}: {e}") from e


@dataclass(frozen=True)
class DomeAction(Action):
    """``do:`` open_slit | close_slit | open_flap | close_flap | track |
    stand | slew (with ``azimuth:`` degrees or ``oppose_sun``)."""

    kind: ClassVar[str] = "dome"

    do: str
    azimuth: float | str | None = None

    _DOS: ClassVar[set[str]] = {
        "open_slit",
        "close_slit",
        "open_flap",
        "close_flap",
        "track",
        "stand",
        "slew",
    }

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "DomeAction":
        check_keys(
            cfg,
            kind="action: dome",
            source=source,
            allowed={"action", "do", "azimuth"},
            required={"do"},
        )
        do = as_choice(
            cfg["do"], cls._DOS, kind="action: dome", key="do", source=source
        )
        azimuth: float | str | None = None
        if do == "slew":
            if "azimuth" not in cfg:
                raise ConfigError(
                    "action: dome: do: slew requires 'azimuth:'", source=source
                )
            raw = cfg["azimuth"]
            if (
                isinstance(raw, str)
                and raw.strip().lower().replace("-", "_") == "oppose_sun"
            ):
                azimuth = "oppose_sun"
            else:
                azimuth = as_float(
                    raw, kind="action: dome", key="azimuth", source=source
                )
        elif "azimuth" in cfg:
            raise ConfigError(
                "action: dome: 'azimuth:' only valid with do: slew", source=source
            )
        return cls(do=do, azimuth=azimuth)

    def to_config(self) -> dict:
        out: dict = {"action": self.kind, "do": self.do}
        if self.azimuth is not None:
            out["azimuth"] = self.azimuth
        return out

    def execute(self, ctx: Context) -> None:
        for dome in ctx.domes:
            if self.do == "open_slit":
                _broadcast(ctx, "Opening dome slit...")
                _guarded_open(
                    ctx, "dome", dome.is_slit_open, dome.open_slit, "open dome slit"
                )
            elif self.do == "close_slit":
                _broadcast(ctx, "Closing dome slit...")
                _guarded_close(
                    ctx, "dome", dome.is_slit_open, dome.close_slit, "close dome slit"
                )
            elif self.do == "open_flap":
                _broadcast(ctx, "Opening dome flap...")
                _guarded_open(
                    ctx, "dome", dome.is_flap_open, dome.open_flap, "open dome flap"
                )
            elif self.do == "close_flap":
                # the dome may keep operating with the flap closed: no flag changes
                if dome.is_flap_open():
                    _broadcast(ctx, "Closing dome flap...")
                    dome.close_flap()
            elif self.do == "track":
                _broadcast(ctx, "Activating dome tracking.")
                dome.track()
            elif self.do == "stand":
                _broadcast(ctx, "Deactivating dome tracking.")
                dome.stand()
            elif self.do == "slew":
                azimuth = self.azimuth
                if azimuth == "oppose_sun":
                    _, sun_az = ctx.site.sunpos()
                    azimuth = (sun_az + 180.0) % 360.0
                dome.stand()
                _broadcast(ctx, f"Moving dome to azimuth {azimuth:.1f}...")
                dome.slew_to_az(float(azimuth))


# --------------------------------------------------------------------------
# telescope
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TelescopeAction(Action):
    """``do:`` unpark | park | open_cover | close_cover | stop_tracking |
    slew (with ``alt:``/``az:`` or ``ra:``/``dec:``)."""

    kind: ClassVar[str] = "telescope"

    do: str
    alt: float | None = None
    az: float | None = None
    ra: str | None = None
    dec: str | None = None

    _DOS: ClassVar[set[str]] = {
        "unpark",
        "park",
        "open_cover",
        "close_cover",
        "stop_tracking",
        "slew",
    }

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "TelescopeAction":
        check_keys(
            cfg,
            kind="action: telescope",
            source=source,
            allowed={"action", "do", "alt", "az", "ra", "dec"},
            required={"do"},
        )
        do = as_choice(
            cfg["do"], cls._DOS, kind="action: telescope", key="do", source=source
        )
        alt = az = ra = dec = None
        if do == "slew":
            if {"alt", "az"} <= set(cfg):
                alt = as_float(
                    cfg["alt"], kind="action: telescope", key="alt", source=source
                )
                az = as_float(
                    cfg["az"], kind="action: telescope", key="az", source=source
                )
            elif {"ra", "dec"} <= set(cfg):
                ra = as_str(
                    cfg["ra"], kind="action: telescope", key="ra", source=source
                )
                dec = as_str(
                    cfg["dec"], kind="action: telescope", key="dec", source=source
                )
            else:
                raise ConfigError(
                    "action: telescope: do: slew requires alt:/az: or ra:/dec:",
                    source=source,
                )
        elif set(cfg) & {"alt", "az", "ra", "dec"}:
            raise ConfigError(
                "action: telescope: coordinates only valid with do: slew", source=source
            )
        return cls(do=do, alt=alt, az=az, ra=ra, dec=dec)

    def to_config(self) -> dict:
        out: dict = {"action": self.kind, "do": self.do}
        for key in ("alt", "az", "ra", "dec"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def execute(self, ctx: Context) -> None:
        for tel in ctx.telescopes:
            if self.do == "unpark":
                if ctx.flags.get_flag("telescope") == Flag.ERROR:
                    raise ActionError(
                        "cannot unpark telescope with ERROR flag set; check instrument"
                    )
                try:
                    _broadcast(ctx, "Unparking telescope...")
                    tel.unpark()
                    ctx.flags.set_flag("telescope", Flag.READY)
                except Exception as e:
                    ctx.flags.set_flag("telescope", Flag.ERROR)
                    _broadcast(ctx, str(e))
                    raise ActionError(f"could not unpark telescope: {e}") from e
            elif self.do == "park":
                try:
                    _broadcast(ctx, "Parking telescope...")
                    tel.park()
                    ctx.flags.set_flag("telescope", Flag.CLOSE)
                except Exception as e:
                    ctx.flags.set_flag("telescope", Flag.ERROR)
                    _broadcast(ctx, str(e))
                    raise ActionError(f"could not park telescope: {e}") from e
            elif self.do == "open_cover":
                if not ctx.flags.can_open("telescope"):
                    _broadcast(
                        ctx,
                        "Cannot open telescope cover due to supervisor constraints.",
                    )
                    raise ActionError(
                        "cannot open telescope cover: supervisor constraints"
                    )
                _broadcast(ctx, "Opening telescope cover.")
                tel.open_cover()
            elif self.do == "close_cover":
                _broadcast(ctx, "Closing telescope cover...")
                tel.close_cover()
            elif self.do == "stop_tracking":
                _broadcast(ctx, "Stopping telescope tracking.")
                tel.stop_tracking()
            elif self.do == "slew":
                if self.alt is not None:
                    _broadcast(
                        ctx, f"Slewing telescope to alt/az {self.alt}/{self.az}."
                    )
                    tel.slew_to_alt_az(self.alt, self.az)
                else:
                    _broadcast(
                        ctx, f"Slewing telescope to ra/dec {self.ra}/{self.dec}."
                    )
                    tel.slew_to_ra_dec(self.ra, self.dec)


# --------------------------------------------------------------------------
# fans and lamps (any chimera Switch at an explicit location)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FanAction(Action):
    """Switch a fan on/off by chimera location, optionally setting speed."""

    kind: ClassVar[str] = "fan"

    do: str  # "switch_on" | "switch_off"
    fan: str
    speed: float | None = None

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "FanAction":
        check_keys(
            cfg,
            kind="action: fan",
            source=source,
            allowed={"action", "do", "fan", "speed"},
            required={"do", "fan"},
        )
        do = as_choice(
            cfg["do"],
            {"switch_on", "switch_off"},
            kind="action: fan",
            key="do",
            source=source,
        )
        speed = None
        if "speed" in cfg:
            if do == "switch_off":
                raise ConfigError(
                    "action: fan: 'speed:' only valid with switch_on", source=source
                )
            speed = as_float(
                cfg["speed"], kind="action: fan", key="speed", source=source
            )
        return cls(
            do=do,
            fan=as_str(cfg["fan"], kind="action: fan", key="fan", source=source),
            speed=speed,
        )

    def to_config(self) -> dict:
        out: dict = {"action": self.kind, "do": self.do, "fan": self.fan}
        if self.speed is not None:
            out["speed"] = self.speed
        return out

    def execute(self, ctx: Context) -> None:
        try:
            fan = ctx.resolve(self.fan)
            if self.do == "switch_on":
                if fan.is_switched_on():
                    _broadcast(ctx, f"Fan {self.fan} is already running.")
                elif fan.switch_on():
                    _broadcast(ctx, f"Fan {self.fan} started.")
                else:
                    _broadcast(ctx, f"Could not start fan {self.fan}.")
                if self.speed is not None:
                    try:
                        _broadcast(
                            ctx, f"Setting fan {self.fan} speed to {self.speed:g}."
                        )
                        fan.set_rotation(self.speed)
                    except Exception:
                        _broadcast(
                            ctx,
                            f"Could not set fan {self.fan} speed to {self.speed:g}.",
                        )
            else:
                if not fan.is_switched_on():
                    _broadcast(ctx, f"Fan {self.fan} is already off.")
                elif fan.switch_off():
                    _broadcast(ctx, f"Fan {self.fan} stopped.")
                else:
                    _broadcast(ctx, f"Could not stop fan {self.fan}.")
        except ActionError:
            raise
        except Exception as e:
            _broadcast(ctx, f"Could not operate fan {self.fan}: {e!r}")
            raise ActionError(f"could not operate fan {self.fan}: {e}") from e


@dataclass(frozen=True)
class LampAction(Action):
    """Switch a lamp on/off by chimera location."""

    kind: ClassVar[str] = "lamp"

    do: str  # "switch_on" | "switch_off"
    lamp: str

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "LampAction":
        check_keys(
            cfg,
            kind="action: lamp",
            source=source,
            allowed={"action", "do", "lamp"},
            required={"do", "lamp"},
        )
        do = as_choice(
            cfg["do"],
            {"switch_on", "switch_off"},
            kind="action: lamp",
            key="do",
            source=source,
        )
        return cls(
            do=do,
            lamp=as_str(cfg["lamp"], kind="action: lamp", key="lamp", source=source),
        )

    def to_config(self) -> dict:
        return {"action": self.kind, "do": self.do, "lamp": self.lamp}

    def execute(self, ctx: Context) -> None:
        try:
            lamp = ctx.resolve(self.lamp)
            if self.do == "switch_on":
                if lamp.is_switched_on():
                    _broadcast(ctx, f"Lamp {self.lamp} is already on.")
                elif lamp.switch_on():
                    _broadcast(ctx, f"Lamp {self.lamp} switched on.")
                else:
                    _broadcast(ctx, f"Could not switch lamp {self.lamp} on.")
            else:
                if not lamp.is_switched_on():
                    _broadcast(ctx, f"Lamp {self.lamp} is already off.")
                elif lamp.switch_off():
                    _broadcast(ctx, f"Lamp {self.lamp} switched off.")
                else:
                    _broadcast(ctx, f"Could not switch lamp {self.lamp} off.")
        except Exception as e:
            _broadcast(ctx, f"Could not operate lamp {self.lamp}: {e!r}")
            raise ActionError(f"could not operate lamp {self.lamp}: {e}") from e


# --------------------------------------------------------------------------
# flags, locks
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SetFlagAction(Action):
    """Set an instrument operation flag by name (``flag: ready``)."""

    kind: ClassVar[str] = "set_flag"

    instrument: str
    flag: Flag

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "SetFlagAction":
        check_keys(
            cfg,
            kind="action: set_flag",
            source=source,
            allowed={"action", "instrument", "flag"},
            required={"instrument", "flag"},
        )
        try:
            flag = Flag.parse(cfg["flag"])
        except ValueError as e:
            raise ConfigError(f"action: set_flag: {e}", source=source) from None
        return cls(
            instrument=as_str(
                cfg["instrument"],
                kind="action: set_flag",
                key="instrument",
                source=source,
            ),
            flag=flag,
        )

    def to_config(self) -> dict:
        return {
            "action": self.kind,
            "instrument": self.instrument,
            "flag": self.flag.value,
        }

    def execute(self, ctx: Context) -> None:
        try:
            ctx.flags.set_flag(self.instrument, self.flag)
        except Exception as e:
            raise ActionError(
                f"could not set {self.instrument} flag to {self.flag}: {e}"
            ) from e


@dataclass(frozen=True)
class LockAction(Action):
    kind: ClassVar[str] = "lock"

    instrument: str
    key: str

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "LockAction":
        check_keys(
            cfg,
            kind="action: lock",
            source=source,
            allowed={"action", "instrument", "key"},
            required={"instrument", "key"},
        )
        return cls(
            instrument=as_str(
                cfg["instrument"], kind="action: lock", key="instrument", source=source
            ),
            key=as_str(cfg["key"], kind="action: lock", key="key", source=source),
        )

    def to_config(self) -> dict:
        return {"action": self.kind, "instrument": self.instrument, "key": self.key}

    def execute(self, ctx: Context) -> None:
        _broadcast(ctx, f"Locking {self.instrument} with key {self.key!r}.")
        try:
            ctx.flags.lock(self.instrument, self.key)
        except Exception as e:
            _broadcast(ctx, str(e))
            raise ActionError(f"could not lock {self.instrument}: {e}") from e


@dataclass(frozen=True)
class UnlockAction(Action):
    kind: ClassVar[str] = "unlock"

    instrument: str
    key: str

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "UnlockAction":
        check_keys(
            cfg,
            kind="action: unlock",
            source=source,
            allowed={"action", "instrument", "key"},
            required={"instrument", "key"},
        )
        return cls(
            instrument=as_str(
                cfg["instrument"],
                kind="action: unlock",
                key="instrument",
                source=source,
            ),
            key=as_str(cfg["key"], kind="action: unlock", key="key", source=source),
        )

    def to_config(self) -> dict:
        return {"action": self.kind, "instrument": self.instrument, "key": self.key}

    def execute(self, ctx: Context) -> None:
        try:
            if ctx.flags.unlock(self.instrument, self.key):
                _broadcast(ctx, f"{self.instrument} unlocked with key {self.key!r}.")
        except StatusUpdateError as e:
            # releasing a key while other keys remain active is not an error
            _broadcast(ctx, str(e))
        except Exception as e:
            _broadcast(ctx, str(e))
            raise ActionError(f"could not unlock {self.instrument}: {e}") from e


# --------------------------------------------------------------------------
# operator interaction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NotifyAction(Action):
    kind: ClassVar[str] = "notify"

    message: str

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "NotifyAction":
        check_keys(
            cfg,
            kind="action: notify",
            source=source,
            allowed={"action", "message"},
            required={"message"},
        )
        return cls(
            message=as_str(
                cfg["message"], kind="action: notify", key="message", source=source
            )
        )

    def to_config(self) -> dict:
        return {"action": self.kind, "message": self.message}

    def execute(self, ctx: Context) -> None:
        _broadcast(ctx, self.message)


@dataclass(frozen=True)
class SendPhotoAction(Action):
    """Broadcast a photo fetched from a URL or local path."""

    kind: ClassVar[str] = "send_photo"

    url: str
    message: str = ""

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "SendPhotoAction":
        check_keys(
            cfg,
            kind="action: send_photo",
            source=source,
            allowed={"action", "url", "message"},
            required={"url"},
        )
        return cls(
            url=as_str(cfg["url"], kind="action: send_photo", key="url", source=source),
            message=str(cfg.get("message", "")),
        )

    def to_config(self) -> dict:
        out: dict = {"action": self.kind, "url": self.url}
        if self.message:
            out["message"] = self.message
        return out

    def execute(self, ctx: Context) -> None:
        if ctx.notifier is None:
            return
        ctx.notifier.broadcast_photo(self.url, self.message)


@dataclass(frozen=True)
class AskOperatorAction(Action):
    """Ask the operator a question and broadcast the answer (informational)."""

    kind: ClassVar[str] = "ask_operator"

    question: str
    timeout: datetime.timedelta = datetime.timedelta(seconds=60)

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "AskOperatorAction":
        check_keys(
            cfg,
            kind="action: ask_operator",
            source=source,
            allowed={"action", "question", "timeout"},
            required={"question"},
        )
        timeout = datetime.timedelta(seconds=60)
        if "timeout" in cfg:
            try:
                timeout = parse_duration(cfg["timeout"], default_unit="s")
            except ValueError as e:
                raise ConfigError(
                    f"action: ask_operator: timeout: {e}", source=source
                ) from None
        return cls(
            question=as_str(
                cfg["question"],
                kind="action: ask_operator",
                key="question",
                source=source,
            ),
            timeout=timeout,
        )

    def to_config(self) -> dict:
        return {
            "action": self.kind,
            "question": self.question,
            "timeout": format_duration(self.timeout),
        }

    def execute(self, ctx: Context) -> None:
        answer = ctx.notifier.ask(self.question, self.timeout)
        _broadcast(ctx, f"Operator answered: {answer}")


# --------------------------------------------------------------------------
# scripts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunScriptAction(Action):
    """Run a shell command; a non-zero exit is reported with the script's output.

    That makes a script usable as an ad-hoc health check - the check lives in
    shell (so it can be site-specific) and only the pass/fail convention is in
    the supervisor.  Pair with ``quiet: true`` for a check that runs every
    cycle, so the operator only hears about it when it fails.
    """

    kind: ClassVar[str] = "run_script"

    _DEFAULT_TIMEOUT: ClassVar[datetime.timedelta] = datetime.timedelta(minutes=10)
    #: cap the output pasted into a notification (Telegram truncates ~4k)
    _MAX_OUTPUT: ClassVar[int] = 1200
    #: a log line can hold more than a notification; still bound pathological output
    _MAX_LOG_OUTPUT: ClassVar[int] = 4000

    path: str
    timeout: datetime.timedelta = _DEFAULT_TIMEOUT
    background: bool = False
    quiet: bool = False

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "RunScriptAction":
        check_keys(
            cfg,
            kind="action: run_script",
            source=source,
            allowed={"action", "path", "timeout", "background", "quiet"},
            required={"path"},
        )
        timeout = cls._DEFAULT_TIMEOUT
        if "timeout" in cfg:
            # bare numbers are seconds, like the operator ask timeouts
            timeout = parse_duration(cfg["timeout"], default_unit="s")
            if timeout <= datetime.timedelta(0):
                raise ConfigError(
                    f"action: run_script: timeout must be positive, got {cfg['timeout']!r}",
                    source=source,
                )
        background = False
        if "background" in cfg:
            background = as_bool(
                cfg["background"],
                kind="action: run_script",
                key="background",
                source=source,
            )
        quiet = False
        if "quiet" in cfg:
            quiet = as_bool(
                cfg["quiet"], kind="action: run_script", key="quiet", source=source
            )
        return cls(
            path=as_str(
                cfg["path"], kind="action: run_script", key="path", source=source
            ),
            timeout=timeout,
            background=background,
            quiet=quiet,
        )

    def to_config(self) -> dict:
        cfg: dict[str, Any] = {"action": self.kind, "path": self.path}
        if self.timeout != self._DEFAULT_TIMEOUT:
            cfg["timeout"] = format_duration(self.timeout)
        if self.background:
            cfg["background"] = True
        if self.quiet:
            cfg["quiet"] = True
        return cfg

    def _run_script(self) -> tuple[int, str]:
        """Run the command in its own process group so a timeout can kill
        the whole tree, not just the shell.  Returns (exit status, output)
        with stderr folded into stdout."""
        proc = subprocess.Popen(
            self.path,
            shell=True,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        try:
            output, _ = proc.communicate(timeout=self.timeout.total_seconds())
            return proc.returncode, output
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.communicate()  # drain the pipe so the child is reaped
            raise

    @staticmethod
    def _tail(text: str, limit: int) -> str:
        text = text.strip()
        if len(text) > limit:
            return "[...]" + text[-limit:]
        return text

    def _failure_message(self, status: int, output: str, what: str = "Script") -> str:
        """Report the exit status, quoting the tail of the output - for a check
        script that is the line explaining what is wrong."""
        message = f"{what} {self.path} exited with status {status}."
        text = self._tail(output, self._MAX_OUTPUT)
        return f"{message}\n{text}" if text else message

    def _log_output(self, ctx: Context, status: int, output: str) -> None:
        """Record the script's output in supervisor.log (never the notifier),
        so a ``quiet`` periodic check still leaves a verifiable trail - e.g. a
        health check can print its measurement and it is kept for later."""
        text = self._tail(output, self._MAX_LOG_OUTPUT)
        if not text:
            return
        level = logging.INFO if status == 0 else logging.WARNING
        ctx.log.log(level, "run_script %s exited %d; output:\n%s", self.path, status, text)

    def _run_and_report(self, ctx: Context) -> None:
        """Background worker: never raises, only broadcasts the outcome."""
        try:
            status, output = self._run_script()
        except subprocess.TimeoutExpired:
            _broadcast(
                ctx,
                f"Background script {self.path} killed after "
                f"{format_duration(self.timeout)} timeout.",
            )
            return
        except Exception as e:
            _broadcast(ctx, f"Background script {self.path} failed: {e}")
            return
        self._log_output(ctx, status, output)
        if status != 0:
            _broadcast(
                ctx, self._failure_message(status, output, what="Background script")
            )

    def execute(self, ctx: Context) -> None:
        executable = self.path.split()[0] if self.path.split() else self.path
        if not os.path.exists(executable):
            _broadcast(ctx, f"Could not find script {self.path} to run.")
            raise ActionError(f"script not found: {self.path}")
        if self.background:
            if not self.quiet:
                _broadcast(ctx, f"Running {self.path} in the background...")
            threading.Thread(
                target=self._run_and_report,
                args=(ctx,),
                name=f"run_script:{executable}",
                daemon=True,
            ).start()
            return
        if not self.quiet:
            _broadcast(ctx, f"Running {self.path}...")
        try:
            status, output = self._run_script()
        except subprocess.TimeoutExpired:
            _broadcast(
                ctx,
                f"Script {self.path} killed after {format_duration(self.timeout)} "
                "timeout.",
            )
            raise ActionError(
                f"script {self.path} timed out after {format_duration(self.timeout)}"
            ) from None
        self._log_output(ctx, status, output)
        if status != 0:
            _broadcast(ctx, self._failure_message(status, output))
            raise ActionError(f"script {self.path} exited with status {status}")


# --------------------------------------------------------------------------
# scheduler / robobs / stop_all
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerAction(Action):
    kind: ClassVar[str] = "scheduler"

    do: str  # "start" | "stop"

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "SchedulerAction":
        check_keys(
            cfg,
            kind="action: scheduler",
            source=source,
            allowed={"action", "do"},
            required={"do"},
        )
        do = as_choice(
            cfg["do"],
            {"start", "stop"},
            kind="action: scheduler",
            key="do",
            source=source,
        )
        return cls(do=do)

    def to_config(self) -> dict:
        return {"action": self.kind, "do": self.do}

    def execute(self, ctx: Context) -> None:
        if self.do == "start":
            ctx.flags.set_flag("scheduler", Flag.OPERATING)
            _broadcast(ctx, "Starting scheduler.")
            for sched in ctx.schedulers:
                sched.start()
        else:
            _broadcast(ctx, "Stopping scheduler.")
            for sched in ctx.schedulers:
                sched.stop()


@dataclass(frozen=True)
class RobObsAction(Action):
    kind: ClassVar[str] = "robobs"

    do: str  # "start" | "stop" | "wake"

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "RobObsAction":
        check_keys(
            cfg,
            kind="action: robobs",
            source=source,
            allowed={"action", "do"},
            required={"do"},
        )
        do = as_choice(
            cfg["do"],
            {"start", "stop", "wake"},
            kind="action: robobs",
            key="do",
            source=source,
        )
        return cls(do=do)

    def to_config(self) -> dict:
        return {"action": self.kind, "do": self.do}

    def execute(self, ctx: Context) -> None:
        for robobs in ctx.robobs:
            if robobs is None:
                continue
            if self.do == "start":
                # Never start the night while the dome or the site is
                # locked: a manual `run RobobsStart` executes responses
                # without the checklist's conditions, and on 2026-07-22 it
                # started the whole queue against a closed, operator-locked
                # dome - autofocus on shut-dome darkness, science slews to
                # nowhere. The locks are somebody's explicit "do not open";
                # starting the robot must respect them from every path.
                locked = [
                    name
                    for name in ("dome", "site")
                    if ctx.flags.get_flag(name) == Flag.LOCK
                ]
                if locked:
                    _broadcast(
                        ctx,
                        f"NOT starting robobs: {', '.join(locked)} locked "
                        f"(release the lock first).",
                    )
                    return
                ctx.flags.set_flag("robobs", Flag.OPERATING)
                _broadcast(ctx, "Starting robobs and waking it up.")
                robobs.start()
                robobs.wake()
            elif self.do == "stop":
                ctx.flags.set_flag("robobs", Flag.READY)
                _broadcast(ctx, "Stopping robobs.")
                robobs.stop()
            else:
                robobs.wake()
            return
        raise ActionError("no robobs controller configured")


@dataclass(frozen=True)
class StopAllAction(Action):
    """Emergency stop: close the scheduler flag, stop robobs, stop the
    scheduler, stop telescope tracking.  Never raises: it always tries every
    step (legacy StopAll semantics)."""

    kind: ClassVar[str] = "stop_all"

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "StopAllAction":
        check_keys(cfg, kind="action: stop_all", source=source, allowed={"action"})
        return cls()

    def to_config(self) -> dict:
        return {"action": self.kind}

    def execute(self, ctx: Context) -> None:
        try:
            ctx.flags.set_flag("scheduler", Flag.CLOSE)
        except Exception as e:
            _broadcast(ctx, str(e))

        try:
            for robobs in ctx.robobs:
                if robobs is not None:
                    robobs.stop()
            if ctx.robobs:
                # the flag must follow the stop (as the plain robobs-stop
                # action does): left at "operating" it blocked make_queue
                # and start_robobs for the whole next day after an
                # operator_lock (2026-07-23)
                ctx.flags.set_flag("robobs", Flag.READY)
                _broadcast(ctx, "Robobs stopped.")
        except Exception as e:
            _broadcast(ctx, f"Error trying to stop robobs: {e}")

        try:
            for sched in ctx.schedulers:
                sched.stop()
            if ctx.schedulers:
                _broadcast(ctx, "Scheduler stopped.")
        except Exception as e:
            _broadcast(ctx, f"Error trying to stop scheduler: {e}")

        try:
            for tel in ctx.telescopes:
                if tel.is_tracking():
                    tel.stop_tracking()
        except NotImplementedError:
            pass
        except Exception as e:
            _broadcast(ctx, str(e))


@dataclass(frozen=True)
class ConfigureSchedulerAction(Action):
    """Replace the chimera scheduler queue with the programs described in a
    YAML file (same format as ``chimera-sched --new -f file.yaml``)."""

    kind: ClassVar[str] = "configure_scheduler"

    file: str

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "ConfigureSchedulerAction":
        check_keys(
            cfg,
            kind="action: configure_scheduler",
            source=source,
            allowed={"action", "file"},
            required={"file"},
        )
        return cls(
            file=as_str(
                cfg["file"],
                kind="action: configure_scheduler",
                key="file",
                source=source,
            )
        )

    def to_config(self) -> dict:
        return {"action": self.kind, "file": self.file}

    def execute(self, ctx: Context) -> None:
        try:
            count = _load_scheduler_programs(self.file)
        except Exception as e:
            _broadcast(ctx, f"Could not configure scheduler from {self.file}: {e!r}")
            try:
                ctx.flags.set_flag("scheduler", Flag.ERROR)
            except Exception:
                pass
            raise ActionError(
                f"could not configure scheduler from {self.file}: {e}"
            ) from e
        ctx.flags.set_flag("scheduler", Flag.READY)
        _broadcast(
            ctx,
            f"Scheduler configured with {count} program(s) from {self.file}. "
            "Restart it to run with the new queue.",
        )


def _load_scheduler_programs(filename: str) -> int:
    """Clear chimera's scheduler queue and load programs from a YAML file.

    Mirrors chimera's own ``chimera-sched`` YAML loader (``cli/sched.py``) so
    files written for that tool work unchanged here.
    """
    import yaml
    from chimera.controllers.scheduler.model import (
        AutoFlat,
        AutoFocus,
        Expose,
        Point,
        PointVerify,
        Program,
        Session,
    )
    from chimera.util.coord import Coord
    from chimera.util.position import Position

    action_types = {
        "autofocus": AutoFocus,
        "autoflat": AutoFlat,
        "pointverify": PointVerify,
        "point": Point,
        "expose": Expose,
    }

    path = os.path.expanduser(filename)
    with open(path) as stream:
        prgconfig = yaml.safe_load(stream)

    def _offset(value: object) -> "Coord":
        try:
            return Coord.from_as(int(value))
        except ValueError:
            return Coord.from_dms(value)

    session = Session()
    for old in session.query(Program).all():
        session.delete(old)
    session.commit()

    programs = []
    for prg in prgconfig["programs"]:
        program = Program()
        for key, value in prg.items():
            if key != "actions" and hasattr(program, key):
                setattr(program, key, value)

        for actconfig in prg["actions"]:
            act = action_types[actconfig["action"]]()
            if actconfig["action"] == "point":
                if {"ra", "dec"} <= set(actconfig):
                    epoch = actconfig.get("epoch", "J2000")
                    act.target_ra_dec = Position.from_ra_dec(
                        actconfig["ra"], actconfig["dec"], epoch
                    )
                elif {"alt", "az"} <= set(actconfig):
                    act.target_alt_az = Position.from_alt_az(
                        actconfig["alt"], actconfig["az"]
                    )
                elif "name" in actconfig:
                    act.target_name = actconfig["name"]
                elif not ({"offset", "dome_az", "dome_tracking"} & set(actconfig)):
                    raise ValueError(f"point action with nothing to do: {actconfig}")
                if "offset" in actconfig:
                    offset = actconfig["offset"]
                    if "north" in offset:
                        act.offset_ns = _offset(offset["north"])
                    elif "south" in offset:
                        act.offset_ns = Coord.from_as(-_offset(offset["south"]).arcsec)
                    if "west" in offset:
                        act.offset_ew = _offset(offset["west"])
                    elif "east" in offset:
                        act.offset_ew = Coord.from_as(-_offset(offset["east"]).arcsec)
                if "pa" in actconfig:
                    act.pa = actconfig["pa"]
                # dome constraints, mainly for dome flats
                if "dome_az" in actconfig:
                    act.dome_az = Coord.from_dms(actconfig["dome_az"])
                if "dome_tracking" in actconfig:
                    tracking = actconfig["dome_tracking"]
                    act.dome_tracking = None if tracking == "None" else tracking
            else:
                for key, value in actconfig.items():
                    if key != "action" and hasattr(act, key):
                        setattr(act, key, value)
            program.actions.append(act)
        programs.append(program)

    session.add_all(programs)
    session.commit()
    return len(programs)
