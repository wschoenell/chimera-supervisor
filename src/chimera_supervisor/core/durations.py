# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Human-readable duration parsing ("2h", "30m", "1h30m", "90s")."""

import datetime
import re

_PART = re.compile(r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[hms])", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def parse_duration(value: object, default_unit: str = "h") -> datetime.timedelta:
    """Parse a duration.

    Accepts ``"2h"``, ``"30m"``, ``"90s"``, combinations (``"1h30m"``) and
    bare numbers, which are interpreted in ``default_unit`` (hours for
    condition offsets, seconds for operator timeouts — matching the units the
    legacy config format used).  A leading sign applies to the whole string.
    """
    if isinstance(value, datetime.timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return datetime.timedelta(seconds=float(value) * _UNIT_SECONDS[default_unit])

    text = str(value).strip()
    if not text:
        raise ValueError("empty duration")

    sign = 1.0
    if text[0] in "+-":
        sign = -1.0 if text[0] == "-" else 1.0
        text = text[1:].strip()

    matches = list(_PART.finditer(text))
    if not matches:
        try:
            return datetime.timedelta(
                seconds=sign * float(text) * _UNIT_SECONDS[default_unit]
            )
        except ValueError:
            raise ValueError(f"not a duration: {value!r}") from None

    consumed = "".join(m.group(0) for m in matches).replace(" ", "")
    if consumed != text.replace(" ", ""):
        raise ValueError(f"not a duration: {value!r}")

    seconds = sum(
        float(m.group("value")) * _UNIT_SECONDS[m.group("unit").lower()]
        for m in matches
    )
    return datetime.timedelta(seconds=sign * seconds)


def format_duration(delta: datetime.timedelta) -> str:
    """Render a timedelta compactly ("-2h", "30m", "1h30m", "90s")."""
    total = delta.total_seconds()
    sign = "-" if total < 0 else ""
    total = abs(total)
    if total == 0:
        return "0s"
    if total < 3600 and total % 60:
        return f"{sign}{total:g}s"
    parts = []
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        parts.append(f"{hours:g}h")
    if minutes:
        parts.append(f"{minutes:g}m")
    if seconds:
        parts.append(f"{seconds:g}s")
    return sign + "".join(parts)
