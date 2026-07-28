# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Small helpers to validate mapping-style config entries."""

from chimera_supervisor.core.exceptions import ConfigError


def check_keys(
    cfg: dict,
    *,
    kind: str,
    source: str,
    allowed: set[str],
    required: set[str] = frozenset(),
) -> None:
    """Fail loudly on unknown or missing keys — typos in safety-critical
    configuration must never be silently ignored."""
    keys = set(cfg)
    unknown = keys - allowed
    if unknown:
        raise ConfigError(
            f"{kind}: unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}",
            source=source,
        )
    missing = required - keys
    if missing:
        raise ConfigError(f"{kind}: missing required key(s) {sorted(missing)}", source=source)


def one_of(cfg: dict, options: set[str], *, kind: str, source: str) -> str:
    """Exactly one of ``options`` must be present in ``cfg``; return it."""
    present = sorted(options & set(cfg))
    if len(present) != 1:
        raise ConfigError(
            f"{kind}: exactly one of {sorted(options)} required, got {present or 'none'}",
            source=source,
        )
    return present[0]


def as_choice(value: object, choices: set[str], *, kind: str, key: str, source: str) -> str:
    text = str(value).strip().lower()
    if text not in choices:
        raise ConfigError(
            f"{kind}: {key}: expected one of {sorted(choices)}, got {value!r}", source=source
        )
    return text


def as_float(value: object, *, kind: str, key: str, source: str) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{kind}: {key}: expected a number, got {value!r}", source=source) from None


def as_int(value: object, *, kind: str, key: str, source: str) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{kind}: {key}: expected an integer, got {value!r}", source=source) from None


def as_str(value: object, *, kind: str, key: str, source: str) -> str:
    if value is None:
        raise ConfigError(f"{kind}: {key}: expected a string, got null", source=source)
    return str(value)


def as_bool(value: object, *, kind: str, key: str, source: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
    raise ConfigError(f"{kind}: {key}: expected a boolean, got {value!r}", source=source)
