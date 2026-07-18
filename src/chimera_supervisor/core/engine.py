# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""The checklist engine.

Pure logic, no threads, no chimera imports: the ``Supervisor`` controller
drives ``run_cycle()`` from its own loop and forwards the observer callbacks
as chimera events.  Everything here is testable with fakes.
"""

import datetime
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from chimera_supervisor.core.actions import Action
from chimera_supervisor.core.checklist import ChecklistItem
from chimera_supervisor.core.conditions import Condition, Result
from chimera_supervisor.core.context import Context
from chimera_supervisor.core.exceptions import ActionError, CheckAbortedError
from chimera_supervisor.persistence.state import StateStore


@dataclass
class Observer:
    """Optional callbacks fired while the engine works (all best-effort)."""

    check_begin: Callable[[ChecklistItem, Condition], None] | None = None
    check_complete: Callable[[ChecklistItem, Condition, Result], None] | None = None
    item_status_changed: Callable[[ChecklistItem, bool], None] | None = None
    response_begin: Callable[[ChecklistItem, Action], None] | None = None
    response_complete: Callable[[ChecklistItem, Action, bool], None] | None = None


class _StoreMemory:
    """MemorySlot backed by the state store, keyed by (item, condition idx)."""

    def __init__(self, store: StateStore, item: str, index: int):
        self._store = store
        self._item = item
        self._index = index

    def get(self) -> datetime.datetime | None:
        return self._store.get_since(self._item, self._index)

    def set(self, value: datetime.datetime | None) -> None:
        self._store.set_since(self._item, self._index, value)


@dataclass
class Engine:
    ctx: Context
    store: StateStore
    observer: Observer = field(default_factory=Observer)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger(__name__))

    def __post_init__(self) -> None:
        self.items: list[ChecklistItem] = []
        self.must_stop = threading.Event()

    # ------------------------------------------------------------------

    def load(self, items: list[ChecklistItem]) -> None:
        names = [item.name for item in items]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"duplicate checklist item names: {sorted(duplicates)}")
        self.items = list(items)
        self.store.prune_items(names)
        self.log.info("checklist loaded: %d item(s) (%d automatic)",
                      len(self.items), sum(1 for i in self.items if i.automatic))

    def item(self, name: str) -> ChecklistItem | None:
        for item in self.items:
            if item.name == name:
                return item
        return None

    def manual_items(self) -> list[str]:
        """Items runnable by hand — historically the inactive ones (they are
        the operator-triggered procedures)."""
        return [item.name for item in self.items if not item.active or not item.automatic]

    # ------------------------------------------------------------------

    def run_cycle(self) -> None:
        """Evaluate every active automatic item once."""
        self.must_stop.clear()
        for item in self.items:
            if self.must_stop.is_set():
                raise CheckAbortedError("checklist cycle aborted")
            if not item.active or not item.automatic:
                continue
            try:
                self.evaluate_item(item)
            except CheckAbortedError:
                raise
            except Exception:
                self.log.exception("error evaluating item %r", item.name)

    def evaluate_item(self, item: ChecklistItem) -> bool:
        """Evaluate one item's conditions and, when they pass and the item's
        run policy says so, fire its responses.  Returns the aggregate
        condition status."""
        now = self.ctx.utcnow()
        status = True
        messages = []

        for index, condition in enumerate(item.conditions):
            if self.must_stop.is_set():
                raise CheckAbortedError(f"aborted while checking {item.name!r}")
            self._notify(self.observer.check_begin, item, condition)
            try:
                result = condition.evaluate(self.ctx, _StoreMemory(self.store, item.name, index))
            except Exception as e:
                self.log.exception("condition %s of %r failed", condition.kind, item.name)
                result = Result(False, f"condition error: {e!r}")
            self._notify(self.observer.check_complete, item, condition, result)
            self.log.debug("[%s] %s: %s — %s", item.name, condition.kind,
                           result.passed, result.message)
            messages.append(result.message)
            if not result.passed:
                status = False
                break

        previous = self.store.item_status(item.name)
        should_run = status and (item.run == "always" or previous is not True)
        changed = previous is None or previous != status

        if changed:
            self._notify(self.observer.item_status_changed, item, status)
        if should_run:
            self.log.info("[%s] conditions passed (%s); running %d response(s)",
                          item.name, "; ".join(messages), len(item.responses))
            self.run_responses(item)
        self.store.update_item(item.name, status, changed=should_run, now=now)
        return status

    def run_responses(self, item: ChecklistItem) -> bool:
        """Run an item's responses, honoring its ``on_error`` policy.
        Returns True when every response succeeded."""
        all_ok = True
        for response in item.responses:
            self._notify(self.observer.response_begin, item, response)
            ok = True
            try:
                response.execute(self.ctx)
            except ActionError as e:
                self.log.warning("[%s] response %s failed: %s", item.name, response.kind, e)
                ok = False
            except Exception:
                self.log.exception("[%s] response %s crashed", item.name, response.kind)
                ok = False
            self._notify(self.observer.response_complete, item, response, ok)
            if not ok:
                all_ok = False
                if item.on_error == "abort":
                    self.log.info("[%s] on_error: abort — stopping response list", item.name)
                    break
        return all_ok

    def run_action(self, name: str) -> bool:
        """Manually trigger an item's responses, skipping its conditions."""
        item = self.item(name)
        if item is None:
            self.log.warning("run_action: no item named %r", name)
            return False
        self.log.info("manually running responses of %r", name)
        try:
            return self.run_responses(item)
        except Exception:
            self.log.exception("manual run of %r failed", name)
            return False

    def abort(self) -> None:
        self.must_stop.set()

    # ------------------------------------------------------------------

    @staticmethod
    def _notify(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logging.getLogger(__name__).exception("engine observer callback failed")
