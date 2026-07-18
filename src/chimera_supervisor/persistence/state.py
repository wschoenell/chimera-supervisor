# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Runtime-state persistence.

Checklist *definitions* live in YAML; this module persists only what must
survive a restart:

- instrument operation flags and their named lock keys,
- per-item last status / timestamps (for run-on-change detection),
- per-condition reference timestamps (for ``for: <duration>`` thresholds).

Plain stdlib ``sqlite3`` — see docs/DESIGN.md for why SQLAlchemy was dropped
here.  All methods are thread-safe (single connection guarded by a lock,
WAL journal).
"""

import datetime
import pathlib
import sqlite3
import threading

from chimera_supervisor.core.exceptions import StatusUpdateError
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instrument_flags (
    instrument  TEXT PRIMARY KEY,
    flag        TEXT NOT NULL,
    last_update TEXT,
    last_change TEXT
);
CREATE TABLE IF NOT EXISTS lock_keys (
    instrument TEXT NOT NULL,
    key        TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    updated    TEXT,
    PRIMARY KEY (instrument, key)
);
CREATE TABLE IF NOT EXISTS item_state (
    name        TEXT PRIMARY KEY,
    last_status INTEGER,
    last_update TEXT,
    last_change TEXT
);
CREATE TABLE IF NOT EXISTS condition_state (
    item    TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    since   TEXT,
    PRIMARY KEY (item, idx)
);
"""


def _iso(moment: datetime.datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(sep=" ")


def _from_iso(text: str | None) -> datetime.datetime | None:
    return None if text is None else datetime.datetime.fromisoformat(text)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class StateStore:
    """Flag board + engine state, persisted in one small SQLite file."""

    def __init__(self, path: str | pathlib.Path = ":memory:"):
        if path != ":memory:":
            pathlib.Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # FlagBoard
    # ------------------------------------------------------------------

    def instruments(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT instrument FROM instrument_flags ORDER BY rowid"
            ).fetchall()
        return [row[0] for row in rows]

    def register_instrument(self, instrument: str) -> None:
        """Make sure an instrument exists on the board (flag UNSET)."""
        now = _iso(_utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO instrument_flags (instrument, flag, last_update, last_change)"
                " VALUES (?, ?, ?, ?)",
                (instrument, Flag.UNSET.value, now, now),
            )

    def get_flag(self, instrument: str) -> Flag:
        with self._lock:
            row = self._conn.execute(
                "SELECT flag FROM instrument_flags WHERE instrument = ?", (instrument,)
            ).fetchone()
        if row is None:
            self.register_instrument(instrument)
            return Flag.UNSET
        return Flag.parse(row[0])

    def _write_flag(self, instrument: str, flag: Flag) -> None:
        now = _iso(_utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO instrument_flags (instrument, flag, last_update, last_change)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(instrument) DO UPDATE SET"
                "   last_change = CASE WHEN flag != excluded.flag THEN excluded.last_update"
                "                      ELSE last_change END,"
                "   flag = excluded.flag,"
                "   last_update = excluded.last_update",
                (instrument, flag.value, now, now),
            )

    def set_flag(self, instrument: str, flag: Flag) -> None:
        """Change a flag.  Locked instruments refuse plain flag changes —
        use unlock() with the right key."""
        current = self.get_flag(instrument)
        if current == Flag.LOCK and flag != Flag.LOCK:
            raise StatusUpdateError(
                f"{instrument} is locked (keys: {self.active_keys(instrument)}); "
                "unlock it before changing its flag"
            )
        self._write_flag(instrument, flag)

    def lock(self, instrument: str, key: str) -> None:
        """Lock an instrument with a named key (adds the key if already
        locked with others)."""
        if not key:
            raise StatusUpdateError(f"cannot lock {instrument} with an empty key")
        now = _iso(_utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO lock_keys (instrument, key, active, updated) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(instrument, key) DO UPDATE SET active = 1, updated = excluded.updated",
                (instrument, key, now),
            )
        self._write_flag(instrument, Flag.LOCK)

    def unlock(self, instrument: str, key: str) -> bool:
        """Release one key.  Returns True when the instrument became fully
        unlocked (flag goes to CLOSE, matching the legacy behavior); False
        when it wasn't locked or didn't hold that key (a no-op, so periodic
        unlock items don't produce noise); raises StatusUpdateError when the
        key was released but other keys still hold the lock."""
        if self.get_flag(instrument) != Flag.LOCK:
            return False
        if key not in self.active_keys(instrument):
            return False
        now = _iso(_utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lock_keys SET active = 0, updated = ? WHERE instrument = ? AND key = ?",
                (now, instrument, key),
            )
        remaining = self.active_keys(instrument)
        if remaining:
            raise StatusUpdateError(
                f"{instrument}: released key {key!r}, still locked with key(s) {remaining}"
            )
        self._write_flag(instrument, Flag.CLOSE)
        return True

    def active_keys(self, instrument: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM lock_keys WHERE instrument = ? AND active = 1 ORDER BY key",
                (instrument,),
            ).fetchall()
        return [row[0] for row in rows]

    def has_key(self, instrument: str, key: str) -> bool:
        return self.get_flag(instrument) == Flag.LOCK and key in self.active_keys(instrument)

    def can_open(self, instrument: str | None = None) -> bool:
        """No instrument given: every registered instrument must be READY or
        OPERATING.  Instrument given: it and the site must be."""
        operational = (Flag.READY, Flag.OPERATING)
        if instrument is None:
            return all(self.get_flag(name) in operational for name in self.instruments())
        return self.get_flag(instrument) in operational and self.get_flag("site") in operational

    # ------------------------------------------------------------------
    # checklist item state
    # ------------------------------------------------------------------

    def item_status(self, name: str) -> bool | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_status FROM item_state WHERE name = ?", (name,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return bool(row[0])

    def item_times(self, name: str) -> tuple[datetime.datetime | None, datetime.datetime | None]:
        """(last_update, last_change)"""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_update, last_change FROM item_state WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None, None
        return _from_iso(row[0]), _from_iso(row[1])

    def update_item(
        self, name: str, status: bool | None, *, changed: bool, now: datetime.datetime
    ) -> None:
        stamp = _iso(now)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO item_state (name, last_status, last_update, last_change)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                "   last_status = excluded.last_status,"
                "   last_update = excluded.last_update,"
                "   last_change = CASE WHEN ? THEN excluded.last_update ELSE last_change END",
                (name, None if status is None else int(status), stamp, stamp if changed else None, changed),
            )

    # ------------------------------------------------------------------
    # condition memory ("for:" thresholds)
    # ------------------------------------------------------------------

    def get_since(self, item: str, index: int) -> datetime.datetime | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT since FROM condition_state WHERE item = ? AND idx = ?", (item, index)
            ).fetchone()
        return _from_iso(row[0]) if row else None

    def set_since(self, item: str, index: int, since: datetime.datetime | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO condition_state (item, idx, since) VALUES (?, ?, ?)"
                " ON CONFLICT(item, idx) DO UPDATE SET since = excluded.since",
                (item, index, _iso(since)),
            )

    def prune_items(self, keep: list[str]) -> None:
        """Drop state of items that no longer exist in the configuration."""
        with self._lock, self._conn:
            placeholders = ",".join("?" for _ in keep) or "''"
            self._conn.execute(
                f"DELETE FROM item_state WHERE name NOT IN ({placeholders})", keep
            )
            self._conn.execute(
                f"DELETE FROM condition_state WHERE item NOT IN ({placeholders})", keep
            )
