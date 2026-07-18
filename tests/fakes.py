# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""Lightweight instrument fakes for the test suite (no chimera bus needed)."""

import datetime

from chimera_supervisor.core.context import Context
from chimera_supervisor.notification import RecordingNotifier
from chimera_supervisor.persistence.state import StateStore

UT = datetime.datetime(2026, 7, 6, 3, 0, 0)  # night at the observatory


class FakeSite:
    def __init__(self, ut=UT):
        self._ut = ut
        self.sunset_time = ut.replace(hour=22, minute=0) - datetime.timedelta(days=1)
        self.sunrise_time = ut.replace(hour=10, minute=0)
        self.sun_alt_az = (-30.0, 60.0)

    def ut(self):
        return self._ut

    def sunset(self, date=None):
        return self.sunset_time

    def sunset_twilight_begin(self, date=None):
        return self.sunset_time + datetime.timedelta(minutes=30)

    def sunset_twilight_end(self, date=None):
        return self.sunset_time + datetime.timedelta(hours=1)

    def sunrise(self, date=None):
        return self.sunrise_time

    def sunrise_twilight_begin(self, date=None):
        return self.sunrise_time - datetime.timedelta(hours=1)

    def sunrise_twilight_end(self, date=None):
        return self.sunrise_time - datetime.timedelta(minutes=30)

    def sunpos(self, date=None):
        return self.sun_alt_az


class FakeDome:
    def __init__(self):
        self.slit_open = False
        self.flap_open = False
        self.mode = "stand"
        self.azimuth = 0.0
        self.calls: list[str] = []

    def is_slit_open(self):
        return self.slit_open

    def is_flap_open(self):
        return self.flap_open

    def open_slit(self):
        self.calls.append("open_slit")
        self.slit_open = True

    def close_slit(self):
        self.calls.append("close_slit")
        self.slit_open = False

    def open_flap(self):
        self.calls.append("open_flap")
        self.flap_open = True

    def close_flap(self):
        self.calls.append("close_flap")
        self.flap_open = False

    def track(self):
        self.calls.append("track")
        self.mode = "track"

    def stand(self):
        self.calls.append("stand")
        self.mode = "stand"

    def slew_to_az(self, az):
        self.calls.append(f"slew_to_az:{az:.1f}")
        self.azimuth = az


class FakeTelescope:
    def __init__(self):
        self.parked = True
        self.cover_open = False
        self.slewing = False
        self.tracking = False
        self.sensors = [("TM1", 10.0), ("FrontRing", 12.0)]
        self.calls: list[str] = []

    def is_parked(self):
        return self.parked

    def is_cover_open(self):
        return self.cover_open

    def is_slewing(self):
        return self.slewing

    def is_tracking(self):
        return self.tracking

    def get_sensors(self):
        return self.sensors

    def park(self):
        self.calls.append("park")
        self.parked = True

    def unpark(self):
        self.calls.append("unpark")
        self.parked = False

    def open_cover(self):
        self.calls.append("open_cover")
        self.cover_open = True

    def close_cover(self):
        self.calls.append("close_cover")
        self.cover_open = False

    def stop_tracking(self):
        self.calls.append("stop_tracking")
        self.tracking = False

    def slew_to_alt_az(self, alt, az):
        self.calls.append(f"slew_to_alt_az:{alt}:{az}")

    def slew_to_ra_dec(self, ra, dec):
        self.calls.append(f"slew_to_ra_dec:{ra}:{dec}")


class FakeWeatherStation:
    def __init__(self, **values):
        self.values = {
            "humidity": 50.0,
            "temperature": 15.0,
            "wind_speed": 5.0,
            "dew_point": 5.0,
            "sky_transparency": 90.0,
            **values,
        }
        self.stale = False

    def get_last_measurement_time(self):
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        if self.stale:
            return (now - datetime.timedelta(hours=2)).isoformat()
        return now.isoformat()

    def humidity(self):
        return self.values["humidity"]

    def temperature(self):
        return self.values["temperature"]

    def wind_speed(self):
        return self.values["wind_speed"]

    def dew_point(self):
        return self.values["dew_point"]

    def sky_transparency(self):
        return self.values["sky_transparency"]


class FakeSwitch:
    """Fan or lamp."""

    def __init__(self):
        self.on = False
        self.rotation = None
        self.calls: list[str] = []

    def is_switched_on(self):
        return self.on

    def switch_on(self):
        self.calls.append("switch_on")
        self.on = True
        return True

    def switch_off(self):
        self.calls.append("switch_off")
        self.on = False
        return True

    def set_rotation(self, speed):
        self.calls.append(f"set_rotation:{speed:g}")
        self.rotation = speed


class FakeStartStop:
    """Scheduler or robobs stand-in."""

    def __init__(self):
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def wake(self):
        self.calls.append("wake")


def make_context(**overrides) -> Context:
    """A fully-populated context over fakes and an in-memory state store."""
    store = overrides.pop("flags", StateStore(":memory:"))
    switches = {}

    def resolve(location):
        if location not in switches:
            switches[location] = FakeSwitch()
        return switches[location]

    ctx = Context(
        site=FakeSite(),
        telescopes=[FakeTelescope()],
        domes=[FakeDome()],
        weather_stations=[FakeWeatherStation()],
        schedulers=[FakeStartStop()],
        robobs=[FakeStartStop()],
        flags=store,
        notifier=RecordingNotifier(),
        resolve=resolve,
        **overrides,
    )
    ctx.switches = switches  # type: ignore[attr-defined]
    for name in ("site", "telescope", "dome", "scheduler", "robobs", "weatherstations"):
        store.register_instrument(name)
    return ctx
