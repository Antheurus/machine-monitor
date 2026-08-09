# When a section is blank or a number is missing

Every collector degrades to an empty result rather than raising, so a missing source hides its
section instead of taking the dashboard down. That is deliberate — and it means a blank section is a
message, not a bug. This is what each one means.

## THERMAL shows no pressure or power draw

`powermetrics` needs root. The tool probes with `sudo -n` and never prompts, so without a sudoers
rule those two fields are omitted and the dashboard prints:

```bash
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/powermetrics
```

**Temperatures in °C are unaffected** — they come from the HID sensors and need nothing. If the
temperature line is also missing, `IOHIDEventSystemClient` did not load, which would be expected on
an Intel Mac or if a future macOS closes the interface.

To disable the powermetrics call entirely, set `use_powermetrics = false` under `[power]`.

## THERMAL shows temperatures but they look wrong

Check that `PMU tcal` is not being included — it is a fixed calibration constant, not a reading.
The grouping logic is in `TemperatureReader.read`.

## DOCKER section absent

`docker` is not on `PATH`, or the daemon is not running. `docker ps` failing is treated as "no
containers", not an error. Turn the section off with `docker = false` under `[sections]`.

## SERVERS RUNNING is empty or missing entries

`lsof` needs no privileges for the current user's processes, but **other users' sockets are invisible
without root**. Ports owned by system daemons may not appear.

Ports listed in `hide_ports` are filtered out — check the config before concluding something is
missing.

## The PROJECT column is empty for a server

The process's cwd could not be read, or it does not sit under any configured `project_roots`. Add
the directory to `project_roots` in the user config. Only paths under a project root count as dev
servers, which is also what gates the duplicate-server and stale-server warnings.

## HISTORY says "not enough samples yet"

Every dashboard run records one sample. A fresh install has nothing to trend. Leave live mode open,
or install the background agent, which records a sample on every check.

If it instead reports `history unavailable`, the SQLite file at
`~/.local/state/machine-monitor/history.db` could not be opened — the message carries the reason.
History failing never affects the live view.

A metric that was not collected on a given run is stored as NULL, not zero, and is skipped by trends.
This is why the CPU-temperature series can have fewer samples than the CPU series: runs with the
thermal section disabled contribute nothing to it rather than contributing a fake 0 °C.

## NET row missing

Both throughput samplers need two readings to produce a rate. The first is taken at startup, so a
single-shot run does show one — but if the interface only just appeared, the row is suppressed rather
than showing a rate derived from a single reading. Loopback is never chosen as the busiest interface.

## `--space` takes half a minute

It runs `du` over caches and every `node_modules`/`.next`/`dist`/`target` under the project roots.
That cost is why it is a separate mode and not a dashboard section — no 3-second refresh can absorb
it. Narrow `project_roots` to speed it up.

## `--reclaim` or `--kill-orphans` refuses

Three separate refusals, each with a different fix:

| Message | Meaning |
|---|---|
| `refusing to … without a terminal` | Not a tty and `--yes` was not passed. |
| `protected process (X)` | On the refusal list in `actions.PROTECTED_NAMES`. |
| `pid now belongs to X, not Y` | The pid was recycled between listing and acting. Re-run. |
| `not permitted — owned by another user` | Needs privileges the tool deliberately does not take. |

`--dry-run` prints exactly what would be signalled and exits 0 without touching anything.

## The background agent is not firing

```bash
machine-monitor --watch-status
```

`plist present but not loaded` means launchd rejected it — check
`~/.local/state/machine-monitor/watch.err`. The check log is `watch.log`, one line per run, including
runs that found nothing.

A condition that has already fired stays quiet for an hour (`COOLDOWN_SECONDS` in `watch.py`), and
its record is cleared as soon as the condition clears, so a recurrence alerts immediately rather than
waiting out a stale cooldown.

## Colors missing, or escape codes printed literally

Color is gated on `NO_COLOR`, `TERM` and `COLORTERM` — deliberately **not** on `isatty()`, which is
false whenever the tool runs under an agent harness while the terminal on the other end renders color
fine. Set `NO_COLOR=1` or pass `--no-color` to disable it.

## Layout looks wrong at an unusual width

Tables compute their flexible column exactly and drop the lowest-priority columns rather than
overflowing. Verified at 80/100/120/160/200 columns with zero overflowing lines — the single
exception is the footer's `lsof` command, which is left unbroken below ~78 columns because breaking
it would make it uncopyable.

If a table looks wrong, `python3 tests/eval_scenarios.py` renders ten synthetic machines including
absurd values and asserts on widths.

## Config changes have no effect

The user file at `~/.config/machine-monitor/config.ini` overrides the shipped
`scripts/config.ini` key by key. Confirm the path with `machine-monitor --config`, and check the
footer — a malformed user file is reported there and the shipped defaults are used instead of
silently ignoring what was written.

Section names must match the shipped file exactly; a key under the wrong section is not applied.
