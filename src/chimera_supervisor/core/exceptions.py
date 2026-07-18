# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Supervisor exceptions."""


class SupervisorError(Exception):
    """Base class for all chimera-supervisor errors."""


class ConfigError(SupervisorError):
    """A checklist configuration file is invalid."""

    def __init__(self, message: str, *, source: str | None = None):
        self.source = source
        super().__init__(f"{source}: {message}" if source else message)


class CheckAbortedError(SupervisorError):
    """A checklist run was aborted by request."""


class ActionError(SupervisorError):
    """A response action could not be completed."""


class StatusUpdateError(SupervisorError):
    """An instrument flag could not be changed (e.g. locked with another key)."""
