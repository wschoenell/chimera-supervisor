# SPDX-License-Identifier: GPL-2.0-or-later
"""Every _watch_* handler must accept exactly what its event publishes.

The bus calls handlers positionally, so a signature that drifts from the
interface raises inside the dispatch pool on *every* occurrence of the event
and the supervisor silently stops reacting to it. Two were wrong on opd-40
(2026-07-21), both unnoticed because the traceback only shows up in
chimera.log:

    Supervisor._watch_tracking_stopped() missing 1 required positional
    argument: 'status'
    Supervisor._watch_slew_begin() takes 2 positional arguments but 4 were
    given

The tracking_stopped one meant the telescope flag was never set back to READY
and the OBJECT_TOO_LOW hook could never fire. (Its telegram broadcast was
removed separately: the scheduler stops tracking at every program end, so it
only ever reported status OK, many times a night.)
"""

import inspect

import pytest
from chimera.interfaces.telescope import TelescopePark, TelescopeSlew, TelescopeTracking

from chimera_supervisor.controllers.supervisor import Supervisor

#: (event owner, event name, supervisor handler) - mirrors the subscriptions
#: made in Supervisor._connect_telescope_events
TELESCOPE_EVENTS = [
    (TelescopeSlew, "slew_begin", "_watch_slew_begin"),
    (TelescopeTracking, "tracking_stopped", "_watch_tracking_stopped"),
    (TelescopePark, "park_complete", "_watch_park_complete"),
    (TelescopePark, "unpark_complete", "_watch_unpark_complete"),
]


def _params(func):
    """Positional parameter names, minus self.

    @event replaces the method with an EventWrapperDispatcher whose own
    signature is (*args, **kwargs); the declared one is on .func.
    """
    func = getattr(func, "func", func)
    return [
        name
        for name, p in inspect.signature(func).parameters.items()
        if name != "self"
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
    ]


@pytest.mark.parametrize(
    "interface,event_name,handler_name",
    TELESCOPE_EVENTS,
    ids=[e[1] for e in TELESCOPE_EVENTS],
)
def test_handler_matches_the_event_it_subscribes_to(
    interface, event_name, handler_name
):
    event = getattr(interface, event_name)
    handler = getattr(Supervisor, handler_name)

    expected = _params(event)
    actual = _params(handler)

    assert actual == expected, (
        f"{handler_name}{tuple(actual)} cannot receive "
        f"{event_name}{tuple(expected)}: the bus calls handlers "
        f"positionally, so every {event_name} would raise TypeError"
    )
