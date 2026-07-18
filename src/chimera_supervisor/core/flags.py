# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Operation flags and statuses used by the supervisor."""

import enum


class InstrumentOperationFlag(enum.Enum):
    """Operational state of a supervised instrument (or the site itself)."""

    UNSET = "unset"  # no information yet
    READY = "ready"  # may open / operate normally
    OPERATING = "operating"  # currently in use
    CLOSE = "close"  # must stay closed / not be operated
    LOCK = "lock"  # locked with one or more named keys
    ERROR = "error"  # in error; condition unknown

    @classmethod
    def parse(cls, value: "str | int | InstrumentOperationFlag") -> "InstrumentOperationFlag":
        """Accept a flag instance, a name ("ready", case-insensitive) or a
        legacy integer index (the order of the old chimera Enum)."""
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise ValueError(f"not an instrument flag: {value!r}")
        if isinstance(value, int):
            return _LEGACY_FLAG_ORDER[value]
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            raise ValueError(f"unknown instrument flag: {value!r}") from None

    def __str__(self) -> str:
        return self.value


# Index order of the legacy chimera.util.enum Enum — legacy configs and the old
# status database store these integers (e.g. "flag: 1" meant READY).
_LEGACY_FLAG_ORDER = [
    InstrumentOperationFlag.UNSET,
    InstrumentOperationFlag.READY,
    InstrumentOperationFlag.OPERATING,
    InstrumentOperationFlag.CLOSE,
    InstrumentOperationFlag.LOCK,
    InstrumentOperationFlag.ERROR,
]


class ResponseStatus(enum.Enum):
    OK = "ok"
    ERROR = "error"
    ABORTED = "aborted"


class EngineState(enum.Enum):
    OFF = "off"
    IDLE = "idle"
    RUNNING = "running"
    SHUTDOWN = "shutdown"
