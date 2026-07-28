# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Legacy (mode-number) checklist format: reader and converter.

The pre-2.0 supervisor described checks and responses with class names and
numeric ``mode`` codes (``type: DomeAction / mode: 6`` meant "switch a fan
off").  This module converts that format into the new human-readable one;
the engine itself only understands the new model.

Every conversion goes through the new parser, so the output is guaranteed to
be a valid new-format checklist.
"""

import datetime
import pathlib

import yaml

from chimera_supervisor.core.checklist import ChecklistItem, parse_item
from chimera_supervisor.core.durations import format_duration
from chimera_supervisor.core.exceptions import ConfigError
from chimera_supervisor.core.flags import InstrumentOperationFlag

_TIME_REFERENCES = {
    0: "sunset",
    1: "sunset",
    2: "sunset_twilight_begin",
    3: "sunset_twilight_end",
    4: "sunrise",
    5: "sunrise_twilight_begin",
    6: "sunrise_twilight_end",
}

_TELESCOPE_STATES = {
    1: "parked",
    -1: "unparked",
    2: "cover_open",
    -2: "cover_closed",
    3: "slewing",
    -3: "not_slewing",
    4: "tracking",
    -4: "not_tracking",
    5: "m1_warmer_than_front_ring",
    -5: "m1_cooler_than_front_ring",
}

_DOME_CHECKS = {
    0: ("slit", "open"),
    1: ("slit", "closed"),
    2: ("flap", "open"),
    3: ("flap", "closed"),
}

_FLAG_TESTS = {0: "is", 1: "is_not", 2: "locked_with_key", 3: "not_locked_with_key"}


class LegacyConversionError(ConfigError):
    pass


def _mode(cfg: dict, default: int = 0) -> int:
    try:
        return int(cfg.get("mode", default))
    except (TypeError, ValueError):
        raise LegacyConversionError(f"invalid mode {cfg.get('mode')!r} in {cfg!r}") from None


def _hours(value: object, default: float = 0.0) -> str | None:
    """Legacy deltaTime (hours, float) → new duration string; None if zero."""
    hours = float(value) if value is not None else default
    if hours == 0.0:
        return None
    return format_duration(datetime.timedelta(hours=hours))


def _flag_name(value: object) -> str:
    return InstrumentOperationFlag.parse(value).value


def _threshold(
    cfg: dict,
    value_key: str,
    mode0_direction: str,
    warnings: list[str],
) -> dict:
    """Common conversion for the legacy weather threshold checks.

    mode 0: bare comparison in ``mode0_direction`` ("above"/"below");
    mode 1: opposite comparison sustained for ``deltaTime`` hours.
    """
    mode = _mode(cfg)
    threshold = float(cfg.get(value_key, 0.0))
    if mode == 0:
        return {mode0_direction: threshold}
    if mode == 1:
        direction = "below" if mode0_direction == "above" else "above"
        hours = float(cfg.get("deltaTime", 0.0) or 0.0)
        # emit for: even when 0 — it preserves the legacy fail-safe behavior
        # on stale weather data (mode-1 checks never passed on stale data)
        return {direction: threshold, "for": f"{hours:g}h"}
    raise LegacyConversionError(f"unsupported mode {mode} for {cfg.get('type')}")


def convert_check(cfg: dict, warnings: list[str]) -> dict:
    """One legacy ``check:`` entry → one new ``conditions:`` entry."""
    kind = str(cfg.get("type", "")).strip().upper()

    if kind == "CHECKTIME":
        mode = _mode(cfg)
        out: dict = {"condition": "time"}
        when = "before" if mode < 0 else "after"
        reference = _TIME_REFERENCES.get(abs(mode))
        if reference is None:
            raw = cfg.get("time")
            if isinstance(raw, datetime.time):
                reference = f"{raw.hour:02d}:{raw.minute:02d}"
            elif raw is not None:
                reference = str(raw)
            else:
                raise LegacyConversionError(f"CheckTime mode {mode} requires 'time'")
        out[when] = reference
        offset = _hours(cfg.get("deltaTime"))
        if offset:
            out["offset"] = offset
        return out

    if kind == "CHECKDOME":
        try:
            part, state = _DOME_CHECKS[_mode(cfg)]
        except KeyError:
            raise LegacyConversionError(f"unsupported CheckDome mode {cfg.get('mode')}") from None
        return {"condition": "dome", part: state}

    if kind == "CHECKTELESCOPE":
        try:
            state = _TELESCOPE_STATES[_mode(cfg)]
        except KeyError:
            raise LegacyConversionError(
                f"unsupported CheckTelescope mode {cfg.get('mode')}"
            ) from None
        return {"condition": "telescope", "state": state}

    if kind == "CHECKWEATHERSTATION":
        mode = _mode(cfg)
        if mode not in (0, 1):
            raise LegacyConversionError(f"unsupported CheckWeatherStation mode {mode}")
        return {
            "condition": "weather_station",
            "station": int(cfg.get("index", 0)),
            "state": "ok" if mode == 0 else "stale",
        }

    if kind == "CHECKHUMIDITY":
        return {"condition": "humidity", **_threshold(cfg, "humidity", "above", warnings)}
    if kind == "CHECKTEMPERATURE":
        return {"condition": "temperature", **_threshold(cfg, "temperature", "below", warnings)}
    if kind == "CHECKWINDSPEED":
        return {"condition": "wind_speed", **_threshold(cfg, "windspeed", "above", warnings)}
    if kind == "CHECKTRANSPARENCY":
        return {"condition": "transparency", **_threshold(cfg, "transparency", "below", warnings)}
    if kind == "CHECKDEW":
        return {"condition": "dew_gap", **_threshold(cfg, "tempdiff", "below", warnings)}
    if kind == "CHECKDEWPOINT":
        return {"condition": "dew_point", **_threshold(cfg, "dewpoint", "below", warnings)}

    if kind == "ASKLISTENER":
        return {
            "condition": "ask_operator",
            "question": str(cfg.get("question", "")),
            "timeout": f"{int(cfg.get('waittime', 60))}s",
        }

    if kind == "CHECKINSTRUMENTFLAG":
        mode = _mode(cfg)
        try:
            test = _FLAG_TESTS[mode]
        except KeyError:
            raise LegacyConversionError(
                f"unsupported CheckInstrumentFlag mode {mode}"
            ) from None
        value = cfg.get("flag", "")
        if test in ("is", "is_not"):
            value = _flag_name(value)
        return {"condition": "flag", "instrument": str(cfg.get("instrument", "")), test: str(value)}

    raise LegacyConversionError(f"unknown legacy check type {cfg.get('type')!r}")


def _fan_from_parameter(parameter: str, do: str) -> dict:
    out: dict = {"action": "fan", "do": do}
    if "," in parameter:
        fan, speed = parameter.split(",", 1)
        out["fan"] = fan.strip()
        out["speed"] = float(speed)
    else:
        out["fan"] = parameter.strip()
    return out


def convert_response(cfg: dict, warnings: list[str]) -> dict:
    """One legacy ``responses:`` entry → one new ``responses:`` entry."""
    kind = str(cfg.get("type", "")).strip().upper()
    parameter = str(cfg.get("parameter", "")).strip()

    if kind == "DOMEACTION":
        mode = _mode(cfg)
        simple = {
            0: "open_slit",
            1: "close_slit",
            2: "open_flap",
            3: "close_flap",
            9: "track",
            10: "stand",
        }
        if mode in simple:
            return {"action": "dome", "do": simple[mode]}
        if mode == 4:
            azimuth: float | str
            if parameter.lower().replace("-", "_") == "oppose_sun":
                azimuth = "oppose_sun"
            else:
                azimuth = float(parameter)
            return {"action": "dome", "do": "slew", "azimuth": azimuth}
        if mode in (5, 6):
            return _fan_from_parameter(parameter, "switch_on" if mode == 5 else "switch_off")
        if mode in (7, 8):
            return {
                "action": "lamp",
                "do": "switch_on" if mode == 7 else "switch_off",
                "lamp": parameter,
            }
        raise LegacyConversionError(f"unsupported DomeAction mode {mode}")

    if kind == "TELESCOPEACTION":
        mode = _mode(cfg)
        simple = {0: "unpark", 1: "park", 2: "open_cover", 3: "close_cover", 7: "stop_tracking"}
        if mode in simple:
            return {"action": "telescope", "do": simple[mode]}
        if mode == 5:
            alt, az = (part.strip() for part in parameter.split(","))
            return {"action": "telescope", "do": "slew", "alt": float(alt), "az": float(az)}
        if mode == 6:
            ra, dec = (part.strip() for part in parameter.split(","))
            return {"action": "telescope", "do": "slew", "ra": ra, "dec": dec}
        if mode in (8, 9):
            return _fan_from_parameter(parameter, "switch_on" if mode == 8 else "switch_off")
        raise LegacyConversionError(f"unsupported TelescopeAction mode {mode}")

    if kind == "DOMEFAN":
        mode = _mode(cfg)
        out = {
            "action": "fan",
            "do": "switch_on" if mode == 0 else "switch_off",
            "fan": str(cfg.get("fan", "/Fan/0")),
        }
        if mode == 0 and cfg.get("speed"):
            out["speed"] = float(cfg["speed"])
        return out

    if kind in ("LOCKINSTRUMENT", "UNLOCKINSTRUMENT"):
        return {
            "action": "lock" if kind == "LOCKINSTRUMENT" else "unlock",
            "instrument": str(cfg.get("instrument", "")),
            "key": str(cfg.get("key", "")),
        }

    if kind == "SETINSTRUMENTFLAG":
        return {
            "action": "set_flag",
            "instrument": str(cfg.get("instrument", "")),
            "flag": _flag_name(cfg.get("flag")),
        }

    if kind == "SENDTELEGRAM":
        return {"action": "notify", "message": str(cfg.get("message", ""))}

    if kind == "SENDPHOTO":
        out = {"action": "send_photo", "url": str(cfg.get("path", ""))}
        if cfg.get("message"):
            out["message"] = str(cfg["message"])
        return out

    if kind == "EXECUTESCRIPT":
        return {"action": "run_script", "path": str(cfg.get("filename", ""))}

    if kind == "CONFIGURESCHEDULER":
        return {"action": "configure_scheduler", "file": str(cfg.get("filename", ""))}

    if kind == "STARTSCHEDULER":
        return {"action": "scheduler", "do": "start"}
    if kind == "STOPSCHEDULER":
        return {"action": "scheduler", "do": "stop"}
    if kind == "STARTROBOBS":
        return {"action": "robobs", "do": "start"}
    if kind == "STOPROBOBS":
        return {"action": "robobs", "do": "stop"}
    if kind == "STOPALL":
        return {"action": "stop_all"}

    if kind == "QUESTION":
        return {
            "action": "ask_operator",
            "question": str(cfg.get("question", "")),
            "timeout": f"{int(cfg.get('waittime', 60))}s",
        }

    raise LegacyConversionError(f"unknown legacy response type {cfg.get('type')!r}")


_LEGACY_ITEM_KEYS = {"name", "eager", "eager_response", "active", "comment", "check", "responses"}


def convert_item(cfg: dict, source: str, warnings: list[str]) -> tuple[str, dict]:
    """One legacy checklist entry → (name, new-format item config)."""
    name = str(cfg.get("name", "")).strip()
    if not name:
        raise LegacyConversionError("legacy item without a name", source=source)
    where = f"{source}: item {name!r}"

    unknown = set(cfg) - _LEGACY_ITEM_KEYS
    if unknown:
        warnings.append(f"{where}: ignoring unknown key(s) {sorted(unknown)}")

    out: dict = {}
    if cfg.get("comment"):
        out["description"] = str(cfg["comment"])
    if cfg.get("active", True) is False:
        out["active"] = False
    if cfg.get("eager", False) is True:
        out["run"] = "always"
    # legacy eager_response=True (default) meant "keep going on failure"
    if cfg.get("eager_response", True) is False:
        out["on_error"] = "abort"

    try:
        conditions = [convert_check(c, warnings) for c in cfg.get("check") or []]
        responses = [convert_response(r, warnings) for r in cfg.get("responses") or []]
    except LegacyConversionError as e:
        raise LegacyConversionError(str(e), source=where) from None

    if conditions:
        out["conditions"] = conditions
    out["responses"] = responses
    return name, out


def convert_document(doc: object, source: str) -> tuple[dict, list[str]]:
    """A legacy YAML document → ({name: item_config}, warnings).

    Duplicate names (which the legacy database allowed) get numeric suffixes.
    """
    warnings: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("checklist"), list):
        raise LegacyConversionError(
            "not a legacy checklist document (expected a 'checklist:' list)", source=source
        )
    body: dict = {}
    for cfg in doc["checklist"]:
        name, item = convert_item(cfg, source, warnings)
        if name in body:
            suffix = 2
            while f"{name}_{suffix}" in body:
                suffix += 1
            warnings.append(f"{source}: duplicate item {name!r} renamed to {name}_{suffix}")
            name = f"{name}_{suffix}"
        body[name] = item
    return {"checklist": body}, warnings


def convert_file(path: str | pathlib.Path) -> tuple[list[ChecklistItem], dict, list[str]]:
    """Convert a legacy file; returns (parsed items, new document, warnings).

    The returned items come from running the converted document through the
    new-format parser, proving the conversion is valid.
    """
    path = pathlib.Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}", source=str(path)) from e
    converted, warnings = convert_document(doc, str(path))
    items = [
        parse_item(name, cfg, str(path)) for name, cfg in converted["checklist"].items()
    ]
    return items, converted, warnings
