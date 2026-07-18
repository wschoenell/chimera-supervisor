# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Checklist items and the (new, human-readable) YAML format.

A checklist file is a mapping of item names to definitions::

    checklist:
      close_on_dew:
        description: Close everything when close to condensation
        conditions:
          - condition: dome
            slit: open
          - condition: dew_gap
            below: 4
        responses:
          - action: stop_all
          - action: telescope
            do: close_cover
          - action: dome
            do: close_slit
          - action: lock
            instrument: dome
            key: dew

(The ``checklist:`` wrapper is optional.)  Items with no ``conditions:`` are
manual actions: they never run automatically and can be triggered by name
(CLI ``run-action`` or Telegram ``/run``).
"""

import pathlib
from dataclasses import dataclass, field

import yaml

from chimera_supervisor.core.actions import Action, parse_action
from chimera_supervisor.core.conditions import Condition, parse_condition
from chimera_supervisor.core.exceptions import ConfigError
from chimera_supervisor.core.parsing import as_bool, as_choice, check_keys

_ITEM_KEYS = {"description", "active", "run", "on_error", "conditions", "responses"}


@dataclass
class ChecklistItem:
    name: str
    conditions: list[Condition] = field(default_factory=list)
    responses: list[Action] = field(default_factory=list)
    description: str = ""
    active: bool = True
    #: "on_change": fire responses only when the aggregate condition status
    #: flips to True; "always": fire on every cycle where conditions hold.
    run: str = "on_change"
    #: "continue": run remaining responses even if one fails (close-down
    #: lists); "abort": stop at the first failing response (open-up lists).
    on_error: str = "continue"
    #: where this item was defined (file path), for error messages
    source: str = ""

    @property
    def automatic(self) -> bool:
        return bool(self.conditions)

    def to_config(self) -> dict:
        """Serialize back to the new YAML format (without the name key)."""
        out: dict = {}
        if self.description:
            out["description"] = self.description
        if not self.active:
            out["active"] = False
        if self.run != "on_change":
            out["run"] = self.run
        if self.on_error != "continue":
            out["on_error"] = self.on_error
        if self.conditions:
            out["conditions"] = [condition.to_config() for condition in self.conditions]
        out["responses"] = [response.to_config() for response in self.responses]
        return out


def parse_item(name: str, cfg: object, source: str) -> ChecklistItem:
    where = f"{source}: item {name!r}"
    if not isinstance(cfg, dict):
        raise ConfigError(f"item {name!r} must be a mapping, got {cfg!r}", source=source)
    check_keys(cfg, kind=f"item {name!r}", source=source, allowed=_ITEM_KEYS)

    conditions_cfg = cfg.get("conditions") or []
    responses_cfg = cfg.get("responses") or []
    if not isinstance(conditions_cfg, list):
        raise ConfigError(f"item {name!r}: 'conditions' must be a list", source=source)
    if not isinstance(responses_cfg, list) or not responses_cfg:
        raise ConfigError(
            f"item {name!r}: 'responses' must be a non-empty list", source=source
        )

    return ChecklistItem(
        name=name,
        description=str(cfg.get("description", "")),
        active=as_bool(cfg.get("active", True), kind=f"item {name!r}", key="active", source=source),
        run=as_choice(
            cfg.get("run", "on_change"),
            {"on_change", "always"},
            kind=f"item {name!r}",
            key="run",
            source=source,
        ),
        on_error=as_choice(
            cfg.get("on_error", "continue"),
            {"continue", "abort"},
            kind=f"item {name!r}",
            key="on_error",
            source=source,
        ),
        conditions=[parse_condition(c, where) for c in conditions_cfg],
        responses=[parse_action(r, where) for r in responses_cfg],
        source=source,
    )


def is_legacy_document(doc: object) -> bool:
    """The legacy format is a list under ``checklist:`` where each entry has
    a ``name`` key; the new format is a mapping of names."""
    return isinstance(doc, dict) and isinstance(doc.get("checklist"), list)


def parse_document(doc: object, source: str) -> list[ChecklistItem]:
    """Parse a new-format YAML document (already loaded) into items."""
    if doc is None:
        return []
    if not isinstance(doc, dict):
        raise ConfigError("top level must be a mapping of item names", source=source)
    if is_legacy_document(doc):
        raise ConfigError(
            "this is a legacy (mode-number) checklist; convert it with "
            "'chimera-supervisor migrate'",
            source=source,
        )
    body = doc.get("checklist", doc)
    if not isinstance(body, dict):
        raise ConfigError("'checklist' must be a mapping of item names", source=source)

    items = []
    for name, cfg in body.items():
        items.append(parse_item(str(name), cfg, source))
    return items


def load_file(path: str | pathlib.Path) -> list[ChecklistItem]:
    path = pathlib.Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}", source=str(path)) from e
    return parse_document(doc, str(path))


def load_directory(directory: str | pathlib.Path) -> list[ChecklistItem]:
    """Load every ``*.yaml`` in a directory; item names must be unique."""
    directory = pathlib.Path(directory).expanduser()
    items: list[ChecklistItem] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("."):
            continue
        for item in load_file(path):
            if item.name in seen:
                raise ConfigError(
                    f"duplicate item {item.name!r} (already defined in {seen[item.name]})",
                    source=str(path),
                )
            seen[item.name] = str(path)
            items.append(item)
    return items


def dump_items(items: list[ChecklistItem]) -> str:
    """Render items as a new-format YAML document."""
    body = {item.name: item.to_config() for item in items}
    return yaml.safe_dump(
        {"checklist": body}, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
