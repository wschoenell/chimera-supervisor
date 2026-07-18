# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Transport-agnostic operator command handling.

A chat integration (Telegram today, Slack or anything else tomorrow) has two
halves:

1. **Transport** — receiving messages/button presses and sending
   text/photos/buttons.  This is the only part a new bot implements
   (see :class:`~chimera_supervisor.telegrambot.TelegramNotifier`).
2. **Commands** — what ``/run``, ``/lock`` etc. actually do.  That lives
   here, in :class:`OperatorCommands`, and is shared by every transport.

A transport must also implement the
:class:`~chimera_supervisor.core.context.Notifier` protocol (``broadcast``,
``broadcast_photo``, ``ask``) so the checklist can reach the operator.
"""

from dataclasses import dataclass, field
from typing import Protocol


class SupervisorPort(Protocol):
    """The narrow slice of the Supervisor that operator interfaces may use."""

    def manual_items(self) -> list[str]: ...

    def run_action(self, name: str) -> bool: ...

    def status_summary(self) -> str: ...

    def lock_instrument(self, instrument: str, key: str) -> None: ...

    def unlock_instrument(self, instrument: str, key: str) -> bool: ...

    def reload_checklist(self) -> str: ...


@dataclass
class Reply:
    """What a command wants shown to the operator.

    ``buttons`` (label, value) pairs, when present, should be rendered as
    pressable buttons; pressing one calls
    :meth:`OperatorCommands.handle_button` with the value.
    """

    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


HELP = """Commands:
/list - list procedures that can be run manually
/run <item> - run a procedure by name
/info - instrument flags and lock keys
/lock <instrument> <key> - lock an instrument
/unlock <instrument> <key> - release a lock key (use with care!)
/reload - reload the checklist configuration files
/help - this message
"""


class OperatorCommands:
    """Shared command dispatcher for operator chat interfaces."""

    def __init__(self, supervisor: SupervisorPort):
        self._supervisor = supervisor

    def handle(self, command: str, args: list[str]) -> Reply:
        """Execute a command (without the leading slash) and describe the
        reply.  Never raises: errors come back as reply text."""
        try:
            handler = getattr(self, f"_cmd_{command.lower().lstrip('/')}", None)
            if handler is None:
                return Reply(f"Unknown command {command!r}.\n{HELP}")
            return handler(args)
        except Exception as e:
            return Reply(f"Command {command!r} failed: {e}")

    def handle_button(self, value: str) -> Reply:
        """Execute a button press produced by a previous Reply."""
        if value.startswith("run:"):
            return self._run(value[len("run:"):])
        return Reply(f"Unknown selection {value!r}.")

    # ------------------------------------------------------------------

    def _cmd_help(self, args: list[str]) -> Reply:
        return Reply(HELP)

    def _cmd_list(self, args: list[str]) -> Reply:
        items = self._supervisor.manual_items()
        if not items:
            return Reply("No procedure available.")
        return Reply(
            "Select a procedure to run:",
            buttons=[(item, f"run:{item}") for item in items],
        )

    def _cmd_run(self, args: list[str]) -> Reply:
        if len(args) != 1:
            return Reply("Usage: /run <item>")
        name = args[0]
        if name not in self._supervisor.manual_items():
            return Reply(f"Procedure {name!r} not available.")
        return self._run(name)

    def _run(self, name: str) -> Reply:
        ok = self._supervisor.run_action(name)
        return Reply(f"{name!r} {'finished' if ok else 'FAILED'}.")

    def _cmd_info(self, args: list[str]) -> Reply:
        return Reply(self._supervisor.status_summary())

    def _cmd_lock(self, args: list[str]) -> Reply:
        if len(args) != 2:
            return Reply("Usage: /lock <instrument> <key>")
        instrument, key = args
        try:
            self._supervisor.lock_instrument(instrument, key)
            return Reply(f"{instrument} locked with key {key}.")
        except Exception as e:
            return Reply(f"Could not lock {instrument}: {e}")

    def _cmd_unlock(self, args: list[str]) -> Reply:
        if len(args) != 2:
            return Reply("Usage: /unlock <instrument> <key>")
        instrument, key = args
        try:
            if self._supervisor.unlock_instrument(instrument, key):
                return Reply(f"{instrument} unlocked (key {key} released).")
            return Reply(f"{instrument} was not locked with key {key}.")
        except Exception as e:
            return Reply(f"Could not unlock {instrument}: {e}")

    def _cmd_reload(self, args: list[str]) -> Reply:
        return Reply(self._supervisor.reload_checklist())
