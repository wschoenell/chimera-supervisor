# T80-South supervisor checklists — inventory and migration plan

A real-world migration example: the complete set of supervisor checklists
in production at the T80-South telescope, converted from the legacy 1.x
(mode-number) format to the 2.0 human-readable format.

All 33 production files parse and convert cleanly with `chimera-supervisor
migrate` (faithful, file-by-file conversions live in `migrated/`; one
embedded webcam credential was stripped for publication). This document
describes what each file does, judges what is worth carrying into the new
format, and defines the curated, **fake-hardware-adapted** set in
`server-testset/` used for the long-term test deployment.

"Active" below means the file contains items with `active: true` (they run
automatically every cycle); "manual" items only run via `/run` or `/list`.

## Automatic safety and operations items

| file | items | what it does | verdict |
| --- | --- | --- | --- |
| `manager_openatsunset.yaml` | **OpenDomeAtSunset** (active, eager), OpposeSun (manual) | The evening open: from sunset−2h, if dome closed, site ready and dome unlocked → set flags, unpark, open slit, dome tracking, fans on, building lamp off, dome opposing sun, open cover, webcam photo | **Translate** — the core of the daily cycle |
| `manager_opendomeflapatnightstart.yaml` | **OpenDomeFlapAtNightStart** (active) | 30 min before twilight end: open flap + cover, fans off, M1 fan off, lamp-off script, start robobs | **Translate** (drop script/robobs) |
| `manager_closeatsunrise.yaml` | **LockDomeOnSunrise** (active) | At sunrise+6m: stop everything, fans on for drying, close cover+slit, dome to 90°, park, lock dome `sunup`, load bias queue into the scheduler and run it, send two webcam photos | **Translate** — closes the daily cycle; scheduler part works against the fake camera with chimera's `sample-sched.yaml`. Note: nothing in the active set ever releases the `sunup` lock (an operator did); the server set adds an automatic dusk unlock so the cycle is autonomous |
| `manager_checktelescopeparked.yaml` | **CheckTelescopeParked** (active) | Telescope parked but flag not `close` → fix the flag | **Translate** — keeps the flag board honest |
| `manager_lockall.yaml` | **CloseOnHumidity**, **CloseOnDew**, **CloseOnWind** (active), TransparencyLock (inactive) | The weather watchdogs: humidity > 85 %, dew gap < 4 °C or wind > 16 m/s (unless already locked with that key) → fans off, stop all, close cover + slit, lock dome with the matching key | **Translate** — the heart of the safety system. Supersedes the older per-condition files below |
| `manager_closeondew.yaml` | CloseOnDew, DailightCloseOnDew (active) | Older split version of the dew watchdog (open-dome close + closed-dome daytime lock) | Skip — superseded by `lockall`; daytime variant folded into the set |
| `manager_closeonwind.yaml` | CloseOnWind, DailightOnWind (active) | Same for wind | Skip — superseded by `lockall` |
| `manager_transplock.yaml` | **TransparencyLock**, **DailightTransparencyLock** (active) | Sky transparency < 35 % → close (if open) and lock the **site** with key `transparency`; daytime variant locks while closed | **Translate** — exercises site-level locks (which veto every `can_open`) |
| `manager_transpUnlock.yaml` | **TestTransparencyUnLock** (active) | Transparency > 40 % sustained 15 min and site locked `transparency` → unlock site, flag ready, all-sky photo | **Translate** (photo → public URL) |
| `manager_unlockdew.yaml` | **UnlockDew** (active, `run: always`) | Dew gap > 5 °C sustained 30 min → release the dome `dew` key | **Translate** — the only automatic unlock at T80S; the server set adds symmetric `humidity`/`wind` unlocks so locks cycle instead of accumulating |
| `manager_unlockall.yaml` | UnlockDew (active) | Same unlock, older copy with a `locked_with_key` guard | Skip — duplicate of `unlockdew` |
| `manager_checkws.yaml` | CheckWS01/02/03 OK/NOTOK ×2, CheckTelescopeParked (active; several duplicate names) | Health of three weather stations: fresh data → flag `ready`, stale → flag `close` | **Translate, collapsed** — server has one fake station, so one ok/stale pair on the `weatherstations` flag. (Duplicated item names in this file were legacy-DB artifacts) |
| `manager_makeskyflat.yaml` | **TakeSkyFlats** (active) | Sunset−9m..+12m, dome open, scheduler idle → open cover, lamp-off script, load skyflat queue, start scheduler | **Translate** (queue file → chimera's `sample-sched.yaml`; drop script; drop the unregistered `SkyFlat` flag guard) |
| `manager_stopshedendofnight.yaml` | **StopSchedEnd**, **TakeSkyFlatsEnd** (active) | Morning-twilight windows: stop everything / close flap + morning flat queue | **Translate** (same adaptations) |
| `manager_coolDome.yaml` | CoolDome (active) | Dome locked with key `sun` → run the dome fans at speed | Production-useful, skip on server (thermal management of a real dome; trivially re-added later) |
| `manager_coolM1.yaml` | CoolM1 (active) | Night, dome open, M1 warmer than front ring (Astelco `get_sensors`) → M1 fan on | Skip on server — FakeTelescope has no temperature sensors; keep for production |
| `manager_sendImageBeforeSkyflat.yaml` | SendPhotoOnSkyFlat (active) | Webcam photo around evening flats | Skip — LAN camera; photo delivery is exercised elsewhere in the set |
| `manager_makequeue.yaml`, `manager_phometriclastnight.yaml` | MakeQueue, PhometricLastNight (active) | Morning batch jobs: build tonight's robobs queue / photometry report, via `/mnt/public` scripts + plots | Skip — robobs + observatory filesystem only |
| `manager_stoprobobsendofnight.yaml` | StopRobObsEnd (active) | Stop robobs in the morning-twilight window | Skip — robobs is not part of this deployment |

## Manual (operator) procedures

| file | items | what it does | verdict |
| --- | --- | --- | --- |
| `manager_opentelescope.yaml` | OpenTelescope | Unpark, open slit, cover, flap | **Translate** |
| `manager_closetelescope.yaml` | CloseTelescope, ParkTelescope | Close/park sequences | **Translate** (ParkTelescope; CloseTelescope is a subset) |
| `manager_operatorLock.yaml` | OperatorLock, OperatorUnLock | Full shutdown + `operator` lock; and its release | **Translate** — exercises multi-key locking with the weather keys |
| `manager_powercut.yaml` | PowerOff | Broadcast a power-cut warning | **Translate** as a notify-only procedure |
| `manager_schedulerinerror.yaml` | SchedulerInError | Stop everything when the scheduler errors (was triggered by a hardcoded hook) | **Translate** as the new `on_scheduler_error` event hook |
| `manager_test.yaml` | TestAction | Scratch item (M1 fan off) | Skip |
| `manager_getAllSky.yaml`, `manager_getInternalImages.yaml`, `manager_getProgress.yaml`, `manager_sendObsPlan.yaml`, `manager_cleanqueuebadweather.yaml` | one each | Fetch LAN webcam/all-sky images, progress plots, clean robobs queue | Skip — LAN cameras, `/mnt/public` scripts, robobs |
| `manager_OpenAndStartRobObs.yaml`, `update.yaml` | OpenAndStartRoboObs + copies of items above | Robobs night start; `update.yaml` is a grab-bag of items duplicated from other files | Skip — robobs; duplicates |

## The server test set (`server-testset/`)

Curated, deduplicated, adapted to the fake observatory (see
[`docs/deploy-fake-observatory.md`](../../docs/deploy-fake-observatory.md)).
Adaptations, all noted per item in the files:

- fans/lamps → `/FakeFan/fake`, `/FakeLamp/fake`; the several physical fans
  collapse to one.
- `run_script` and robobs actions dropped (no observatory filesystem, no
  robobs).
- LAN webcam photos → one representative `send_photo` with a public URL
  (exercises Telegram photo delivery); the rest dropped.
- Scheduler queue files → chimera's own `sample-sched.yaml`, which the fake
  camera can execute.
- **Additions for autonomy** (marked `ADDED`): `unlock_sunup_at_dusk`
  (releases the morning `sunup` lock so the evening open can proceed),
  `unlock_humidity` and `unlock_wind` (mirror `UnlockDew`, so weather locks
  cycle instead of accumulating forever).
- Item names converted to snake_case; originals noted in each description.

With the fake weather station (humidity = 40·cos(hour·π/12)+60 %, dew point
−10 °C, wind 10 m/s, transparency 84 %) and the T80S site ephemeris
(sunset ≈ 21:57 UT, sunrise ≈ 11:45 UT in July), a typical day looks like:

- ~20:35 UT — humidity crosses 85 % → `close_on_humidity` locks the dome
  (it is still closed: the lock simply blocks the evening open).
- ~21:50 UT — dew gap drops below 4 °C → `close_on_dew` adds the `dew` key.
- ~02:11 / ~03:25 UT — dew gap and humidity recover; after their sustained
  `for:` windows, `unlock_dew` / `unlock_humidity` release the keys.
- ~04:10 UT — dome unlocked, still night → `open_dome_at_sunset` opens the
  observatory (unpark, slit, fan, cover, oppose sun) and Telegram gets the
  photo + notifications.
- 11:45 UT — `close_at_sunrise` shuts down, parks, locks `sunup`, loads the
  sample queue into the scheduler and starts it (fake camera "exposes").
- ~21:27 UT — `unlock_sunup_at_dusk` releases the morning lock and the
  cycle repeats.
- Wind (10 < 16) and transparency (84 > 35) never trigger: they soak the
  quiet path. Station-health items set the `weatherstations` flag `ready`
  and would flip it to `close` if the fake ever went stale.
- Manual procedures (`park_telescope`, `operator_lock/unlock`,
  `open_telescope`, `power_off`) stay available from Telegram `/list`.

To deploy this set on a fake-observatory test server: copy
`server-testset/*.yaml` into the supervisor's `checklist_dir`, run
`chimera-supervisor validate <dir>`, then `chimera-supervisor reload`.
Bootstrap note: the evening open requires the `site` flag to be `ready`
(at T80S an operator procedure set it once); set it once with
`set_flag` or a one-off checklist item.
