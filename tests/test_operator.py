# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""OperatorCommands is transport-agnostic: these tests are exactly what a
Slack (or any other) bot would exercise."""

from chimera_supervisor.core.exceptions import StatusUpdateError
from chimera_supervisor.operator import OperatorCommands


class FakeSupervisor:
    def __init__(self):
        self.ran: list[str] = []
        self.locked: list[tuple[str, str]] = []

    def manual_items(self):
        return ["park_telescope", "open_up"]

    def run_action(self, name):
        self.ran.append(name)
        return name != "open_up"

    def status_summary(self):
        return "- dome: ready"

    def lock_instrument(self, instrument, key):
        if instrument == "nope":
            raise StatusUpdateError("no such instrument")
        self.locked.append((instrument, key))

    def unlock_instrument(self, instrument, key):
        return (instrument, key) in self.locked

    def reload_checklist(self):
        return "checklist loaded: 2 item(s)"


def make():
    supervisor = FakeSupervisor()
    return OperatorCommands(supervisor), supervisor


def test_list_returns_buttons():
    commands, _ = make()
    reply = commands.handle("list", [])
    assert reply.buttons == [
        ("park_telescope", "run:park_telescope"),
        ("open_up", "run:open_up"),
    ]


def test_button_press_runs_item():
    commands, supervisor = make()
    reply = commands.handle_button("run:park_telescope")
    assert supervisor.ran == ["park_telescope"]
    assert "finished" in reply.text


def test_run_command_checks_availability():
    commands, supervisor = make()
    assert "not available" in commands.handle("run", ["wibble"]).text
    assert "Usage" in commands.handle("run", []).text
    assert "FAILED" in commands.handle("run", ["open_up"]).text
    assert supervisor.ran == ["open_up"]


def test_info_lock_unlock_reload():
    commands, supervisor = make()
    assert "dome: ready" in commands.handle("info", []).text
    assert "locked" in commands.handle("lock", ["dome", "dew"]).text
    assert supervisor.locked == [("dome", "dew")]
    assert "unlocked" in commands.handle("unlock", ["dome", "dew"]).text
    assert "not locked" in commands.handle("unlock", ["dome", "x"]).text
    assert "loaded" in commands.handle("reload", []).text


def test_errors_never_raise():
    commands, _ = make()
    assert "Could not lock" in commands.handle("lock", ["nope", "k"]).text
    assert "Unknown command" in commands.handle("frobnicate", []).text
    assert "Unknown selection" in commands.handle_button("zap:x").text


def test_help_lists_commands():
    commands, _ = make()
    text = commands.handle("help", []).text
    for command in ("/list", "/run", "/info", "/lock", "/unlock", "/reload"):
        assert command in text
