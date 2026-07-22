# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Checklist condition catalog.

Every condition is a small dataclass parsed from a mapping like::

    - condition: time
      after: sunset
      offset: -2h

Conditions are pure: ``evaluate(ctx, memory)`` reads instruments through the
:class:`~chimera_supervisor.core.context.Context` and returns a
:class:`Result`; the only state they may keep between evaluations is the
single timestamp held in the :class:`MemorySlot` the engine hands them
(used by ``for:`` duration thresholds).
"""

import abc
import datetime
import math
import re
from dataclasses import dataclass
from typing import Any, ClassVar, NamedTuple, Protocol

from chimera_supervisor.core.context import Context, naive_utc
from chimera_supervisor.core.durations import format_duration, parse_duration
from chimera_supervisor.core.exceptions import ConfigError
from chimera_supervisor.core.flags import InstrumentOperationFlag
from chimera_supervisor.core.parsing import (
    as_choice,
    as_float,
    as_int,
    as_str,
    check_keys,
    one_of,
)


class Result(NamedTuple):
    passed: bool
    message: str


class MemorySlot(Protocol):
    """One persisted timestamp per condition instance (see engine)."""

    def get(self) -> datetime.datetime | None: ...

    def set(self, value: datetime.datetime | None) -> None: ...


class TransientMemory:
    """In-memory MemorySlot (tests, and conditions that don't persist)."""

    def __init__(self) -> None:
        self._value: datetime.datetime | None = None

    def get(self) -> datetime.datetime | None:
        return self._value

    def set(self, value: datetime.datetime | None) -> None:
        self._value = value


_REGISTRY: dict[str, type["Condition"]] = {}


def condition_kinds() -> list[str]:
    return sorted(_REGISTRY)


def parse_condition(cfg: object, source: str) -> "Condition":
    """Build a condition from one entry of a ``conditions:`` list."""
    if not isinstance(cfg, dict):
        raise ConfigError(
            f"condition entry must be a mapping, got {cfg!r}", source=source
        )
    if "condition" not in cfg:
        raise ConfigError(
            f"condition entry missing 'condition:' key: {cfg!r}", source=source
        )
    kind = str(cfg["condition"]).strip().lower()
    try:
        cls = _REGISTRY[kind]
    except KeyError:
        raise ConfigError(
            f"unknown condition {kind!r}; available: {condition_kinds()}", source=source
        ) from None
    return cls._from_config(cfg, source)


class Condition(abc.ABC):
    """Base class; subclasses register themselves under their ``kind``."""

    kind: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if kind:
            _REGISTRY[kind] = cls

    @classmethod
    @abc.abstractmethod
    def _from_config(cls, cfg: dict, source: str) -> "Condition": ...

    @abc.abstractmethod
    def to_config(self) -> dict:
        """Inverse of ``parse_condition`` (used by the migration tool)."""

    @abc.abstractmethod
    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result: ...


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

_TIME_REFERENCES = {
    "sunset",
    "sunset_twilight_begin",
    "sunset_twilight_end",
    "sunrise",
    "sunrise_twilight_begin",
    "sunrise_twilight_end",
}
_HHMM = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")


@dataclass(frozen=True)
class TimeCondition(Condition):
    """True when now is before/after a solar event (or fixed UT time) plus an
    offset, e.g. ``after: sunset, offset: -2h`` = "within two hours of sunset
    or later"."""

    kind: ClassVar[str] = "time"

    when: str  # "after" | "before"
    reference: str  # one of _TIME_REFERENCES or "HH:MM" (UT)
    offset: datetime.timedelta = datetime.timedelta(0)

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "TimeCondition":
        check_keys(
            cfg,
            kind="condition: time",
            source=source,
            allowed={"condition", "after", "before", "offset"},
        )
        when = one_of(cfg, {"after", "before"}, kind="condition: time", source=source)
        reference = str(cfg[when]).strip().lower()
        if reference not in _TIME_REFERENCES and not _HHMM.match(reference):
            raise ConfigError(
                f"condition: time: {when}: expected one of {sorted(_TIME_REFERENCES)} "
                f'or "HH:MM" (UT), got {cfg[when]!r}',
                source=source,
            )
        offset = datetime.timedelta(0)
        if "offset" in cfg:
            try:
                offset = parse_duration(cfg["offset"], default_unit="h")
            except ValueError as e:
                raise ConfigError(
                    f"condition: time: offset: {e}", source=source
                ) from None
        return cls(when=when, reference=reference, offset=offset)

    def to_config(self) -> dict:
        out: dict = {"condition": self.kind, self.when: self.reference}
        if self.offset:
            out["offset"] = format_duration(self.offset)
        return out

    def _reference_time(self, ctx: Context) -> datetime.datetime:
        site = ctx.site
        now = ctx.utcnow()
        match = _HHMM.match(self.reference)
        if match:
            return now.replace(
                hour=int(match["h"]), minute=int(match["m"]), second=0, microsecond=0
            )
        if self.reference == "sunset_twilight_end":
            # computed from the sunset instant, as the legacy TimeHandler did
            sunset = naive_utc(site.sunset(now.date()))
            reference = naive_utc(site.sunset_twilight_end(sunset))
        else:
            reference = naive_utc(getattr(site, self.reference)(now.date()))

        # The site returns the NEXT occurrence of the event, so once the UTC
        # date rolls over an EVENING reference jumps to tomorrow's - and a
        # condition like `after: sunset` can never be true again for the
        # night already under way. Seen 2026-07-22: open_dome_at_sunset went
        # false at midnight, so a night interrupted after that could not be
        # resumed and the dome stayed shut. Morning references are exempt:
        # during the night the coming sunrise is genuinely ahead of us, and
        # rolling one back would fire lock_dome_on_sunrise in the afternoon.
        if self.reference.startswith("sunset") and reference - now > datetime.timedelta(
            hours=12
        ):
            reference -= datetime.timedelta(days=1)
        return reference

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        now = ctx.utcnow()
        reference = self._reference_time(ctx) + self.offset
        if self.when == "after":
            passed = now > reference
        else:
            passed = now < reference
        state = "passed" if now > reference else "still in the future"
        return Result(
            passed,
            f"reference time {self.reference}{format_duration(self.offset) if self.offset else ''}"
            f" ({reference}) {state}; now {now}",
        )


# --------------------------------------------------------------------------
# dome / telescope / weather station health
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DomeCondition(Condition):
    """``slit: open|closed`` or ``flap: open|closed``."""

    kind: ClassVar[str] = "dome"

    part: str  # "slit" | "flap"
    state: str  # "open" | "closed"

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "DomeCondition":
        check_keys(
            cfg,
            kind="condition: dome",
            source=source,
            allowed={"condition", "slit", "flap"},
        )
        part = one_of(cfg, {"slit", "flap"}, kind="condition: dome", source=source)
        state = as_choice(
            cfg[part],
            {"open", "closed"},
            kind="condition: dome",
            key=part,
            source=source,
        )
        return cls(part=part, state=state)

    def to_config(self) -> dict:
        return {"condition": self.kind, self.part: self.state}

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        dome = ctx.domes[0]
        is_open = dome.is_slit_open() if self.part == "slit" else dome.is_flap_open()
        passed = is_open if self.state == "open" else not is_open
        return Result(passed, f"dome {self.part} is {'open' if is_open else 'closed'}")


_TELESCOPE_STATES = {
    "parked": lambda tel: tel.is_parked(),
    "unparked": lambda tel: not tel.is_parked(),
    "cover_open": lambda tel: tel.is_cover_open(),
    "cover_closed": lambda tel: not tel.is_cover_open(),
    "slewing": lambda tel: tel.is_slewing(),
    "not_slewing": lambda tel: not tel.is_slewing(),
    "tracking": lambda tel: tel.is_tracking(),
    "not_tracking": lambda tel: not tel.is_tracking(),
}


def _m1_front_ring_delta(tel: Any) -> float:
    sensors = dict((info[0], info[1]) for info in tel.get_sensors())
    return sensors["TM1"] - sensors["FrontRing"]


@dataclass(frozen=True)
class TelescopeCondition(Condition):
    """Telescope state, e.g. ``state: parked`` / ``state: not_tracking``.

    ``m1_warmer_than_front_ring`` / ``m1_cooler_than_front_ring`` compare the
    primary-mirror and front-ring temperature sensors (Astelco-style
    ``get_sensors()``).
    """

    kind: ClassVar[str] = "telescope"

    state: str

    _STATES: ClassVar[set[str]] = set(_TELESCOPE_STATES) | {
        "m1_warmer_than_front_ring",
        "m1_cooler_than_front_ring",
    }

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "TelescopeCondition":
        check_keys(
            cfg,
            kind="condition: telescope",
            source=source,
            allowed={"condition", "state"},
            required={"state"},
        )
        state = as_choice(
            cfg["state"],
            cls._STATES,
            kind="condition: telescope",
            key="state",
            source=source,
        )
        return cls(state=state)

    def to_config(self) -> dict:
        return {"condition": self.kind, "state": self.state}

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        tel = ctx.telescopes[0]
        if self.state in _TELESCOPE_STATES:
            passed = bool(_TELESCOPE_STATES[self.state](tel))
            return Result(passed, f"telescope state check '{self.state}': {passed}")
        delta = _m1_front_ring_delta(tel)
        if self.state == "m1_warmer_than_front_ring":
            passed = delta > 0
        else:
            passed = delta < 0
        return Result(passed, f"M1 - FrontRing temperature difference: {delta:+.2f} C")


def _last_measurement(ws: Any) -> datetime.datetime | None:
    """Return the station's last measurement time as naive UTC, or None."""
    try:
        stamp = ws.get_last_measurement_time()
    except Exception:
        return None
    if stamp is None:
        return None
    if isinstance(stamp, datetime.datetime):
        return naive_utc(stamp)
    try:
        return naive_utc(datetime.datetime.fromisoformat(str(stamp)))
    except ValueError:
        return None


def _is_fresh(ws: Any, ctx: Context) -> bool:
    last = _last_measurement(ws)
    if last is None:
        return False
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return now - last < ctx.max_weather_age


@dataclass(frozen=True)
class WeatherStationCondition(Condition):
    """Health of one weather station: ``state: ok`` (fresh data) or
    ``state: stale`` (no fresh data)."""

    kind: ClassVar[str] = "weather_station"

    station: int
    state: str  # "ok" | "stale"

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "WeatherStationCondition":
        check_keys(
            cfg,
            kind="condition: weather_station",
            source=source,
            allowed={"condition", "station", "state"},
        )
        station = as_int(
            cfg.get("station", 0),
            kind="condition: weather_station",
            key="station",
            source=source,
        )
        state = as_choice(
            cfg.get("state", "ok"),
            {"ok", "stale"},
            kind="condition: weather_station",
            key="state",
            source=source,
        )
        return cls(station=station, state=state)

    def to_config(self) -> dict:
        return {"condition": self.kind, "station": self.station, "state": self.state}

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        try:
            ws = ctx.weather_stations[self.station]
            fresh = _is_fresh(ws, ctx)
        except Exception:
            fresh = False
        passed = fresh if self.state == "ok" else not fresh
        return Result(
            passed,
            f"weather station {self.station} data is "
            f"{'fresh' if fresh else 'stale or unavailable'}",
        )


# --------------------------------------------------------------------------
# weather thresholds
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherThresholdCondition(Condition):
    """Common machinery for the weather threshold conditions.

    Exactly one of ``above:`` / ``below:`` sets the comparison.  With
    ``for: <duration>`` the comparison must hold continuously for that long
    (tracked across restarts); after passing, the timer restarts.

    Fail-safe rule when no station has fresh data (same as legacy behavior):
    a bare threshold passes (assume the weather is bad), a ``for:`` threshold
    fails (never e.g. reopen the dome based on stale data).
    """

    kind: ClassVar[str] = ""
    quantity: ClassVar[str] = ""
    unit: ClassVar[str] = ""

    above: float | None = None
    below: float | None = None
    duration: datetime.timedelta | None = None

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "WeatherThresholdCondition":
        label = f"condition: {cls.kind}"
        check_keys(
            cfg,
            kind=label,
            source=source,
            allowed={"condition", "above", "below", "for"},
        )
        which = one_of(cfg, {"above", "below"}, kind=label, source=source)
        threshold = as_float(cfg[which], kind=label, key=which, source=source)
        duration = None
        if "for" in cfg:
            try:
                duration = parse_duration(cfg["for"], default_unit="h")
            except ValueError as e:
                raise ConfigError(f"{label}: for: {e}", source=source) from None
        return cls(
            above=threshold if which == "above" else None,
            below=threshold if which == "below" else None,
            duration=duration,
        )

    def to_config(self) -> dict:
        out: dict = {"condition": self.kind}
        if self.above is not None:
            out["above"] = self.above
        if self.below is not None:
            out["below"] = self.below
        if self.duration is not None:
            out["for"] = format_duration(self.duration)
        return out

    def _read(self, ws: Any) -> float:
        """Read the measured quantity from one station (may raise)."""
        raise NotImplementedError

    def _read_first_fresh(self, ctx: Context) -> float | None:
        for ws in ctx.weather_stations:
            try:
                if _is_fresh(ws, ctx):
                    value = float(self._read(ws))
                    # NaN is the chimera convention for "this station does not
                    # measure this quantity" - skip it, never compare with it
                    # (NaN comparisons are silently false).
                    if math.isnan(value):
                        continue
                    return value
            except Exception:
                continue
        return None

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        measured = self._read_first_fresh(ctx)
        if measured is None:
            if self.duration is None:
                return Result(
                    True, f"no fresh {self.quantity} data — assuming unsafe (fail-safe)"
                )
            return Result(False, f"no fresh {self.quantity} data — holding (fail-safe)")

        if self.above is not None:
            holds = measured > self.above
            comparison = f"{self.quantity} {measured:.2f}{self.unit} above {self.above:.2f}{self.unit}"
        else:
            holds = measured < self.below
            comparison = f"{self.quantity} {measured:.2f}{self.unit} below {self.below:.2f}{self.unit}"
        if not holds:
            comparison = comparison.replace(" above ", " not above ").replace(
                " below ", " not below "
            )

        if self.duration is None:
            return Result(holds, comparison)

        now = ctx.utcnow()
        if not holds:
            memory.set(now)
            return Result(False, comparison)
        since = memory.get()
        if since is None:
            memory.set(now)
            return Result(
                False, f"{comparison}; started timing {format_duration(self.duration)}"
            )
        elapsed = now - since
        if elapsed >= self.duration:
            memory.set(now)  # restart the timer after a successful pass
            return Result(True, f"{comparison} for {format_duration(elapsed)}")
        return Result(
            False,
            f"{comparison} for {format_duration(elapsed)}"
            f" (< {format_duration(self.duration)})",
        )


class HumidityCondition(WeatherThresholdCondition):
    kind: ClassVar[str] = "humidity"
    quantity: ClassVar[str] = "humidity"
    unit: ClassVar[str] = "%"

    def _read(self, ws: Any) -> float:
        return ws.humidity()


class TemperatureCondition(WeatherThresholdCondition):
    kind: ClassVar[str] = "temperature"
    quantity: ClassVar[str] = "temperature"
    unit: ClassVar[str] = " C"

    def _read(self, ws: Any) -> float:
        return ws.temperature()


class WindSpeedCondition(WeatherThresholdCondition):
    kind: ClassVar[str] = "wind_speed"
    quantity: ClassVar[str] = "wind speed"
    unit: ClassVar[str] = " m/s"

    def _read(self, ws: Any) -> float:
        return ws.wind_speed()


class TransparencyCondition(WeatherThresholdCondition):
    kind: ClassVar[str] = "transparency"
    quantity: ClassVar[str] = "sky transparency"
    unit: ClassVar[str] = "%"

    def _read(self, ws: Any) -> float:
        return ws.sky_transparency()


class DewPointCondition(WeatherThresholdCondition):
    kind: ClassVar[str] = "dew_point"
    quantity: ClassVar[str] = "dew point"
    unit: ClassVar[str] = " C"

    def _read(self, ws: Any) -> float:
        return ws.dew_point()


class DewGapCondition(WeatherThresholdCondition):
    """Gap between ambient temperature and dew point (T - T_dew), in Celsius.
    ``below: 4`` means "within 4 degrees of condensation"."""

    kind: ClassVar[str] = "dew_gap"
    quantity: ClassVar[str] = "temperature-dew point gap"
    unit: ClassVar[str] = " C"

    def _read(self, ws: Any) -> float:
        return ws.temperature() - ws.dew_point()


# --------------------------------------------------------------------------
# flags and operator interaction
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlagCondition(Condition):
    """Instrument operation flag test: ``is:``/``is_not:`` a flag name, or
    ``locked_with_key:``/``not_locked_with_key:`` a lock key."""

    kind: ClassVar[str] = "flag"

    instrument: str
    test: str  # "is" | "is_not" | "locked_with_key" | "not_locked_with_key"
    value: str

    _TESTS: ClassVar[set[str]] = {
        "is",
        "is_not",
        "locked_with_key",
        "not_locked_with_key",
    }

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "FlagCondition":
        check_keys(
            cfg,
            kind="condition: flag",
            source=source,
            allowed={"condition", "instrument"} | cls._TESTS,
            required={"instrument"},
        )
        test = one_of(cfg, cls._TESTS, kind="condition: flag", source=source)
        if test in ("is", "is_not"):
            try:
                value = InstrumentOperationFlag.parse(cfg[test]).value
            except (ValueError, IndexError):
                raise ConfigError(
                    f"condition: flag: unknown instrument flag {cfg[test]!r}",
                    source=source,
                ) from None
        else:
            value = as_str(cfg[test], kind="condition: flag", key=test, source=source)
        instrument = as_str(
            cfg["instrument"], kind="condition: flag", key="instrument", source=source
        )
        return cls(instrument=instrument, test=test, value=value)

    def to_config(self) -> dict:
        return {
            "condition": self.kind,
            "instrument": self.instrument,
            self.test: self.value,
        }

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        flags = ctx.flags
        if self.test in ("is", "is_not"):
            current = flags.get_flag(self.instrument)
            wanted = InstrumentOperationFlag.parse(self.value)
            passed = (current == wanted) if self.test == "is" else (current != wanted)
            return Result(passed, f"{self.instrument} flag is {current}")
        locked = flags.has_key(self.instrument, self.value)
        passed = locked if self.test == "locked_with_key" else not locked
        return Result(
            passed,
            f"{self.instrument} is {'locked' if locked else 'not locked'}"
            f" with key {self.value!r}",
        )


@dataclass(frozen=True)
class AskOperatorCondition(Condition):
    """Ask the operator (via the notifier) and pass only on a positive answer.
    Times out to "no"."""

    kind: ClassVar[str] = "ask_operator"

    question: str
    timeout: datetime.timedelta = datetime.timedelta(seconds=60)

    @classmethod
    def _from_config(cls, cfg: dict, source: str) -> "AskOperatorCondition":
        check_keys(
            cfg,
            kind="condition: ask_operator",
            source=source,
            allowed={"condition", "question", "timeout"},
            required={"question"},
        )
        timeout = datetime.timedelta(seconds=60)
        if "timeout" in cfg:
            try:
                timeout = parse_duration(cfg["timeout"], default_unit="s")
            except ValueError as e:
                raise ConfigError(
                    f"condition: ask_operator: timeout: {e}", source=source
                ) from None
        question = as_str(
            cfg["question"],
            kind="condition: ask_operator",
            key="question",
            source=source,
        )
        return cls(question=question, timeout=timeout)

    def to_config(self) -> dict:
        return {
            "condition": self.kind,
            "question": self.question,
            "timeout": format_duration(self.timeout),
        }

    def evaluate(self, ctx: Context, memory: MemorySlot) -> Result:
        answer = ctx.notifier.ask(self.question, self.timeout)
        passed = str(answer).strip().upper() in {"OK", "YES", "Y"}
        return Result(passed, f"operator answered {answer!r}")
