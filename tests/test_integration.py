# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""End-to-end: the Supervisor controller inside a real chimera Manager/Bus
with fake instruments (no network beyond localhost, no hardware)."""

import random
import threading
import time

import pytest


@pytest.fixture
def observatory(tmp_path):
    from chimera.core.bus import Bus
    from chimera.core.manager import Manager
    from chimera.core.site import Site
    from chimera.instruments.fakedome import FakeDome
    from chimera.instruments.faketelescope import FakeTelescope
    from chimera.instruments.fakeweatherstation import FakeWeatherStation

    from chimera_supervisor.controllers.supervisor import Supervisor

    checklist_dir = tmp_path / "checklist"
    checklist_dir.mkdir()
    (checklist_dir / "test.yaml").write_text(
        """
checklist:
  notify_when_parked:
    conditions:
      - condition: telescope
        state: parked
    responses:
      - action: notify
        message: telescope is parked
  park_it:
    responses:
      - action: telescope
        do: park
"""
    )

    port = random.randint(20000, 60000)
    bus = Bus(f"tcp://127.0.0.1:{port}")
    manager = Manager(bus=bus)
    threading.Thread(target=bus.run_forever, daemon=True).start()
    time.sleep(0.5)

    manager.add_class(
        Site,
        "obs",
        {"name": "T80S", "latitude": "-30:10:04", "longitude": "-70:48:20", "altitude": 2187},
        start=True,
    )
    manager.add_class(FakeTelescope, "tel", start=True)
    manager.add_class(FakeDome, "dome", {"telescope": "/FakeTelescope/tel"}, start=True)
    manager.add_class(FakeWeatherStation, "ws", start=True)
    manager.add_class(
        Supervisor,
        "main",
        {
            "site": "/Site/obs",
            "telescope": "/FakeTelescope/tel",
            "dome": "/FakeDome/dome",
            "camera": None,
            "weatherstations": "/FakeWeatherStation/ws",
            "checklist_dir": str(checklist_dir),
            "state_db": str(tmp_path / "state.db"),
        },
        start=True,
    )

    proxy = manager.get_proxy(f"tcp://127.0.0.1:{port}/Supervisor/main")
    yield proxy

    manager.shutdown()
    bus.shutdown()


def test_supervisor_over_the_bus(observatory):
    proxy = observatory

    assert proxy.items() == ["notify_when_parked", "park_it"]
    assert proxy.manual_items() == ["park_it"]
    assert "telescope: unset" in proxy.status_summary()

    # manual procedure over RPC: parks the fake telescope, flag goes close
    assert proxy.run_action("park_it") is True
    assert proxy.get_flag("telescope") == "close"

    # named locks over RPC
    proxy.lock_instrument("dome", "testkey")
    assert proxy.get_flag("dome") == "lock"
    assert proxy.unlock_instrument("dome", "testkey") is True
    assert proxy.get_flag("dome") == "close"

    # live reload
    assert "2 item(s)" in proxy.reload_checklist()

    # an automatic cycle runs without blowing up
    assert proxy.wakeup() is True
    time.sleep(1.5)
