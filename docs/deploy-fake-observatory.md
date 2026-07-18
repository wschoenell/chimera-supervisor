# Deploying a fake observatory for long-term testing

This guide sets up a complete chimera system — site, fake instruments, the
chimera scheduler and the **Supervisor** — on a remote Linux server,
running unattended for days or weeks as a soak test. No hardware is
touched: every instrument is one of chimera's `Fake*` classes.

Every command below was verified against chimera 0.2 and
chimera-supervisor 2.0.

What you get out of a soak test:

- the supervisor opens the fake dome around sunset and closes it after
  sunrise, every day, using real ephemeris for the configured site;
- the fake weather station's humidity follows a daily sine curve that
  crosses the close threshold, so weather closes / lock / timed-reopen
  cycles are exercised daily;
- long-run concerns (memory growth, log rotation, state-database growth,
  thread leaks, event-subscription stability) surface before the code goes
  anywhere near a telescope.

## 1. Server prerequisites

A Linux box you can ssh into. Install [uv](https://docs.astral.sh/uv/) and
git; uv fetches Python itself, so no system Python is needed:

```bash
ssh testbox
curl -LsSf https://astral.sh/uv/install.sh | sh   # then re-login or source ~/.profile
```

## 2. Get the code and build one environment

Chimera discovers plugins by scanning installed packages named `chimera_*`,
so **all packages must live in the same virtualenv** as the `chimera`
executable.

```bash
mkdir -p ~/chimera-test/src && cd ~/chimera-test/src
git clone https://github.com/astroufsc/chimera.git
git clone https://github.com/astroufsc/chimera-supervisor.git

cd ~/chimera-test
uv venv --python 3.13 .venv          # chimera needs >= 3.13; plain `uv venv` may pick an older python
uv pip install --python .venv/bin/python -e src/chimera
uv pip install --python .venv/bin/python --no-sources -e src/chimera-supervisor
```

Notes:

- install `chimera` **first**; the plugin's `chimera` requirement is then
  satisfied locally instead of from PyPI;
- `--no-sources` is required for the plugin: its `pyproject.toml` pins
  `chimera` to a development path (`../chimera`) via `[tool.uv.sources]`,
  which conflicts with the editable install you just did;
- editable installs (`-e`) mean an update is just `git pull` + restart.

Sanity check:

```bash
.venv/bin/python -c "import chimera, chimera_supervisor; print('ok')"
.venv/bin/chimera --version        # 0.2
```

## 3. Configuration

### 3.1 `~/.chimera/chimera.config`

This is chimera's default configuration path, so no `--config` flag is
needed anywhere (chimera creates a sample there on first run — replace it):

```yaml
chimera:
  host: 127.0.0.1        # keep the bus on localhost; access is via ssh
  port: 7666

site:
  name: T80S
  latitude: "-30:10:04.31"
  longitude: "-70:48:20.48"
  altitude: 2187
  flat_alt: 80
  flat_az: 10

telescope:
  name: fake
  type: FakeTelescope

camera:
  name: fake
  type: FakeCamera

filterwheel:
  name: fake
  type: FakeFilterWheel
  filters: "U B V R I"

focuser:
  name: fake
  type: FakeFocuser

dome:
  name: fake
  type: FakeDome
  mode: stand
  telescope: /FakeTelescope/fake

fan:
  name: fake
  type: FakeFan

lamp:
  name: fake
  type: FakeLamp

weatherstation:
  name: fake
  type: FakeWeatherStation

controller:
  # the camera resolves /ImageServer/0 to decide where exposures are
  # saved; without it every scheduler exposure fails
  - type: ImageServer
    name: fake
    images_dir: ~/chimera-test/images
    httpd: False
    autoload: False

  - type: Scheduler
    name: sched
    telescope: /FakeTelescope/fake
    camera: /FakeCamera/fake
    dome: /FakeDome/fake
    site: /Site/T80S

  - type: Supervisor
    name: main
    site: /Site/T80S
    telescope: /FakeTelescope/fake
    dome: /FakeDome/fake
    camera: /FakeCamera/fake
    scheduler: /Scheduler/sched
    weatherstations: /FakeWeatherStation/fake
    checklist_dir: /home/YOU/chimera-test/checklist
    state_db: /home/YOU/chimera-test/state.db
    freq: 0.02                    # one checklist cycle every 50 s
    # telegram_token: "..."       # optional: real operator notifications
    # telegram_broadcast_ids: "..."
    # telegram_listen_ids: "..."
```

Use absolute paths for `checklist_dir`/`state_db` (no `~` expansion in the
config).

### 3.2 The soak checklist — `~/chimera-test/checklist/soak.yaml`

This exercises time conditions (real ephemeris), flag logic, guarded
open/close, fans, weather thresholds with `for:` timers, locks, and a
manual procedure:

```yaml
checklist:
  open_at_sunset:
    description: Soak test - open the fake dome in the evening
    on_error: abort
    conditions:
      - condition: time
        after: sunset
        offset: -30m
      - condition: time
        before: sunrise
      - condition: dome
        slit: closed
      - condition: flag
        instrument: dome
        is_not: lock
      - condition: humidity
        below: 85
    responses:
      - action: set_flag
        instrument: site
        flag: ready
      - action: set_flag
        instrument: telescope
        flag: ready
      - action: set_flag
        instrument: dome
        flag: ready
      - action: telescope
        do: unpark
      - action: dome
        do: open_slit
      - action: fan
        do: switch_on
        fan: /FakeFan/fake
      - action: notify
        message: soak - observatory opened

  close_at_sunrise:
    description: Soak test - close everything in the morning
    conditions:
      - condition: time
        after: sunrise
        offset: 6m
      - condition: dome
        slit: open
    responses:
      - action: stop_all
      - action: dome
        do: close_slit
      - action: fan
        do: switch_off
        fan: /FakeFan/fake
      - action: telescope
        do: park
      - action: notify
        message: soak - observatory closed

  close_on_humidity:
    description: Soak test - weather close (fake humidity crosses 85% daily)
    conditions:
      - condition: dome
        slit: open
      - condition: humidity
        above: 85
    responses:
      - action: stop_all
      - action: dome
        do: close_slit
      - action: lock
        instrument: dome
        key: humidity

  reopen_after_humidity:
    description: Soak test - reopen when dry for 30 minutes
    conditions:
      - condition: flag
        instrument: dome
        locked_with_key: humidity
      - condition: humidity
        below: 80
        for: 30m
    responses:
      - action: unlock
        instrument: dome
        key: humidity
      - action: set_flag
        instrument: dome
        flag: ready

  park_now:
    description: Manual procedure for operators
    responses:
      - action: stop_all
      - action: dome
        do: close_slit
      - action: telescope
        do: park
```

(`FakeWeatherStation` humidity is `40·cos(hour·π/12) + 60`, so it crosses
85% around midnight UT every day — the weather close and the 30-minute
timed reopen both fire daily. `reopen_after_humidity` also proves the
`for:` timer survives restarts: it is persisted in `state_db`.)

Validate before running — typos are hard errors by design:

```bash
cd ~/chimera-test
.venv/bin/chimera-supervisor validate checklist/
# OK    checklist/soak.yaml: 5 item(s)
```

## 4. First run, by hand

```bash
.venv/bin/chimera -v
```

Look for `System up and running.` and no `error starting /...` lines, then
from a second ssh session:

```bash
cd ~/chimera-test
.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main items
# open_at_sunset
# close_at_sunrise
# close_on_humidity
# reopen_after_humidity
# park_now  [manual]

.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main info
.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main run park_now
.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main lock dome maintenance
.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main unlock dome maintenance
```

Ctrl-C the foreground `chimera` when satisfied.

## 5. Run it long-term (systemd user service)

`~/.config/systemd/user/chimera-fake.service`:

```ini
[Unit]
Description=chimera fake observatory (soak test)
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/chimera-test
ExecStart=%h/chimera-test/.venv/bin/chimera -v
Restart=on-failure
RestartSec=10
# fail fast if something leaks instead of taking the box down:
MemoryMax=2G

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now chimera-fake
loginctl enable-linger $USER      # keep it running after you log out
```

Console output goes to the journal: `journalctl --user -u chimera-fake -f`.

No systemd? A tmux session works: `tmux new -s chimera` then run the
foreground command from §4 and detach (`Ctrl-b d`).

## 6. Operating and monitoring over ssh

Run the CLIs **on the server** (one-off commands work fine through ssh):

```bash
ssh testbox '~/chimera-test/.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main info'
```

Don't try to reach the bus through an `ssh -L` tunnel: chimera's bus
replies by dialing back to the caller's own address, which a plain forward
tunnel can't route. For real remote interaction configure the Telegram bot
(`telegram_*` keys in the Supervisor block) — `/info`, `/list`, `/run`,
`/lock`, `/unlock`, `/reload` then work from anywhere, and the soak
checklist's `notify` actions message you at every open/close.

What to check during the soak:

```bash
# daily rhythm: opened/closed at the right ephemeris times?
journalctl --user -u chimera-fake --since "-2 days" | grep "soak -"

# supervisor's own logs (rotating):
tail -F ~/.chimera/supervisor.log

# errors and stack traces:
journalctl --user -u chimera-fake -p warning --since "-1 day"
grep -c "Traceback" ~/.chimera/supervisor.log

# memory growth (note RSS once a day; it should plateau):
systemctl --user status chimera-fake | grep Memory
ps -o rss=,etime= -p $(pgrep -f "bin/chimera ")

# persisted state: flags, lock keys, item status, "for:" timers
sqlite3 ~/chimera-test/state.db 'SELECT * FROM instrument_flags;'
sqlite3 ~/chimera-test/state.db 'SELECT * FROM item_state;'
```

A healthy week looks like: one `soak - observatory opened` ~30 min before
sunset and one `soak - observatory closed` ~6 min after sunrise per day,
one humidity close + one timed reopen per day, flat memory, no tracebacks.

Editing the checklist while it runs is supported:

```bash
vim ~/chimera-test/checklist/soak.yaml
.venv/bin/chimera-supervisor validate checklist/       # always validate first
.venv/bin/chimera-supervisor --port 7666 --supervisor /Supervisor/main reload
```

A file with errors is rejected as a whole and the previous configuration
stays active.

## 7. Updating the deployment

```bash
cd ~/chimera-test/src/chimera-supervisor && git pull   # same for the others
systemctl --user restart chimera-fake
```

(Editable installs pick up the new code on restart; re-run the
`uv pip install` line from §2 only if dependencies changed.)

## 8. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Chimera is already running on this machine` | port 7666 already bound — another instance or a stale process (`pgrep -f bin/chimera`) |
| `uv venv` errors about Python < 3.13 | pass `--python 3.13` (the default interpreter may be older) |
| `Requirements contain conflicting URLs for package chimera` | you forgot `--no-sources` when installing the plugin |
| CLI says `could not resolve proxy` | wrong `--port`/`--supervisor` location, or the service is down (`systemctl --user status chimera-fake`) |
| controller missing from `items`/errors at boot | check `journalctl --user -u chimera-fake` for `error starting /...`; plugin packages must be installed in the *same* venv as `chimera` |
| checklist edits ignored | you edited but didn't `reload`; or validation failed — run `validate` and read the error |
