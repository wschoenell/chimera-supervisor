# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Execution context handed to conditions and actions.

The context is the only channel through which the checklist reaches the
outside world (instrument proxies, flag board, notifier).  Tests provide a
context built from fakes; the ``Supervisor`` controller builds one from live
chimera proxies.
"""

import datetime
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from chimera_supervisor.core.flags import InstrumentOperationFlag


class FlagBoard(Protocol):
    """Instrument operation flags and named locks (see persistence.state)."""

    def instruments(self) -> Sequence[str]: ...

    def get_flag(self, instrument: str) -> InstrumentOperationFlag: ...

    def set_flag(self, instrument: str, flag: InstrumentOperationFlag) -> None: ...

    def lock(self, instrument: str, key: str) -> None: ...

    def unlock(self, instrument: str, key: str) -> bool: ...

    def has_key(self, instrument: str, key: str) -> bool: ...

    def can_open(self, instrument: str | None = None) -> bool: ...


class Notifier(Protocol):
    """Operator notification channel (Telegram in production)."""

    def broadcast(self, message: str) -> None: ...

    def broadcast_photo(self, url: str, message: str = "") -> None: ...

    def ask(self, question: str, timeout: datetime.timedelta) -> str:
        """Ask the operator a yes/no question; return the answer (or "no" on
        timeout / when nobody is listening)."""
        ...


@dataclass
class Context:
    """Everything a condition or action may need at evaluation time.

    Instrument attributes are lists because the supervisor supports several
    instances per role (e.g. multiple weather stations); an empty list means
    the role is not configured.
    """

    site: Any = None
    telescopes: list[Any] = field(default_factory=list)
    domes: list[Any] = field(default_factory=list)
    cameras: list[Any] = field(default_factory=list)
    weather_stations: list[Any] = field(default_factory=list)
    schedulers: list[Any] = field(default_factory=list)
    robobs: list[Any] = field(default_factory=list)

    flags: FlagBoard | None = None
    notifier: Notifier | None = None

    #: diagnostic log; the controller passes its own logger so action output
    #: lands in supervisor.log (never the operator notifier / Telegram).
    log: logging.Logger = field(
        default_factory=lambda: logging.getLogger("chimera_supervisor.checklist")
    )

    #: measurements older than this are considered stale
    max_weather_age: datetime.timedelta = datetime.timedelta(minutes=10)

    #: resolve an arbitrary chimera location ("/FanClass/name") to a proxy
    resolve: Callable[[str], Any] = lambda location: None

    #: run another checklist item's responses by name (used by future
    #: call_action response; also exposed to the CLI/bot)
    run_action: Callable[[str], bool] = lambda name: False

    def utcnow(self) -> datetime.datetime:
        """Naive-UTC 'now', taken from the site when available so that
        simulated sites work."""
        if self.site is not None:
            return naive_utc(self.site.ut())
        return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def naive_utc(value: datetime.datetime) -> datetime.datetime:
    """Normalize a datetime to naive UTC (site methods may return aware)."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.UTC).replace(tzinfo=None)
    return value
