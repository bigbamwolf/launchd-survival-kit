# launchd survival kit

**Keep a small Mac alive under a herd of always on automation.**

Three small, dependency free Python tools for people who run many launchd agents, headless browsers and AI agents on one machine, often an 8GB laptop that was never meant for it.

| Tool | What it does |
|---|---|
| `bootguard` | Revives launchd agents that silently failed to load at login. Config driven roster, never touches a job you killed on purpose. |
| `reaper` | Watchdog that kills runaway automation processes: orphaned scripts spinning a core, idle automation Chrome, bloated renderers, leftover agent sessions. Also frees disk when it runs low. |
| `ram_relief` | Kills nothing, reboots nothing. Frees regenerable caches when disk is low and notifies you when a danger is sustained. |
| `global_slot` | A machine wide one at a time queue so heavy browser jobs never launch at the same instant and run the machine out of memory. Wrap any launchd job with it, no code changes needed. |

Every number and rule in here came from a real incident on a real machine. The [Lessons](#lessons) section is the part worth reading even if you never run the code.

## The problem

You start with three launchd jobs. A year later you have a hundred and fifty: scrapers, pollers, sync jobs, AI agents on a timer. Each one is fine alone. Together they fail in ways no single job shows you:

* After a reboot, a third of them are simply not loaded. Nothing logs it.
* Two browser jobs fire on the same minute, memory overcommits, the swap file eats the disk, and the machine freezes or kernel panics.
* A crashed run leaves an orphaned Chrome or a Python spinner at 90 percent CPU for eleven hours. Its launchd label never runs again until it dies.
* Your watchdog itself stalls under the exact load it was built to handle.

This kit is the set of guards that ended those failures on one overloaded Mac.

## Install

Requires macOS and the system `python3`. No pip packages.

```bash
git clone https://github.com/bigbamwolf/launchd-survival-kit.git
cd launchd-survival-kit
./install.sh                         # dry: writes config, renders plists into ./build, loads nothing
$EDITOR ~/.config/launchd-survival-kit/config.json
./install.sh --load                  # copies plists to ~/Library/LaunchAgents and bootstraps them
```

Use your own label prefix with `--prefix com.yourname.lsk`. Remove with `./install.sh --unload`, which boots the jobs out AND disables them so a reboot does not bring them back.

## Layout

```
launchd-survival-kit/
├── config.example.json        one config for every tool, every key optional
├── install.sh
├── kit/
│   ├── kitconfig.py           config loader shared by the tools
│   ├── bootguard.py
│   ├── reaper.py
│   ├── ram_relief.py
│   ├── global_slot.py
│   └── with_global_slot.py    wrap any command in the global slot
└── launchd/
    ├── bootguard.plist        RunAtLoad + every 30 min
    ├── reaper.plist           RunAtLoad + every 3 min
    ├── ram-relief.plist       wall clock, every 15 min
    └── example-wrapped-job.plist
```

Config is read from `$LSK_CONFIG`, then `~/.config/launchd-survival-kit/config.json`, then `config.json` in the repo. State and logs go to `~/.local/state/launchd-survival-kit` (override with `state_dir` or `$LSK_STATE_DIR`).

## bootguard

```bash
kit/bootguard.py              # reconcile the roster now
kit/bootguard.py --dry-run    # show what would be revived
kit/bootguard.py --fallen     # every plist on disk that launchd has not loaded
```

List the labels you want alive in `bootguard.revive`. On each run it compares that roster with `launchctl list` and bootstraps anything missing. It skips a label when its plist is gone, its target script is gone, it is disabled in launchd, or its script calls `launchctl bootout` on itself (a self terminating job must never be revived, or the guard fights it forever). Every run appends one line to `bootguard.jsonl`.

It runs from its own plist AND from the end of every reaper tick, because the guard is subject to the same load failure it fixes.

## reaper

```bash
kit/reaper.py --dry-run       # log what would die, kill nothing
```

| Rule | Fires on | Default grace |
|---|---|---|
| `zombie_python` | orphan (ppid 1) script from stdin or a temp dir, over 20 percent CPU, no children | 2 ticks |
| `idle_cdp_chrome` | automation Chrome with a debugging port and zero connected clients | 2 ticks |
| `orphan_headless` | `chrome-headless-shell` reparented to launchd | 2 ticks |
| `fat_renderer` | automation renderer over 1500MB (800MB in emergency) | immediate |
| `fat_headless` | headless renderer over 1200MB, only while swap used is over 5500MB | immediate |
| `orphan_sidecar` | helper processes matching `sidecar_pattern` left behind by a dead parent | 2 ticks |
| `idle_session` | finished agent sessions matching `idle_session.process_pattern`, only under real memory pressure | 2 ticks |
| `stale_tab` | blank and new tab pages inside automation browsers, closed over CDP | n/a |
| `disk_reap` | your configured temp targets past an age floor, only when disk is low | n/a |

Safety model:

* Only Chrome launched with a `--user-data-dir` under `automation_profile_prefixes` is ever considered. Your own browser is structurally out of scope.
* `protected_patterns` shields a process AND everything descended from it, by walking the parent chain. A protected script's browser and renderer are safe even though their own command lines never name the script.
* PIDs listed in `<state_dir>/protect_pids.txt` are never killed.
* Killing a renderer only crashes a tab. The browser lives, the driver sees a crashed page and reloads.
* Every kill goes to `reaper_kills.jsonl`.

## ram_relief

Measures swap as a percentage of the current swap cap, free disk and free memory. When disk is under `disk_relief_gb` it runs your `reclaim_commands`. When a danger survives `sustain_ticks` consecutive ticks it runs `notify_command` with the message in `$LSK_MESSAGE`. The default posts a macOS notification; point it at a Slack, Discord or ntfy webhook with `curl` if you are away from the machine. Test it with `kit/ram_relief.py --test-notify`.

## global_slot

In Python:

```python
import sys; sys.path.insert(0, "/path/to/launchd-survival-kit/kit")
import global_slot as G

with G.heavy("nightly-scrape"):              # waits its turn
    run_browser_job()

try:
    with G.heavy("poller", wait=False):      # a scheduler skips instead of piling up
        poll()
except G.SlotBusy:
    sys.exit(0)

with G.heavy("chat-reply", priority="hi", timeout=270):   # jumps the queue
    reply()
```

Any command, from a plist, without touching its code:

```xml
<key>ProgramArguments</key>
<array>
    <string>/usr/bin/python3</string>
    <string>/path/to/kit/with_global_slot.py</string>
    <string>--skip-if-busy</string>
    <string>nightly-scrape</string>
    <string>/usr/bin/python3</string>
    <string>/path/to/scrape.py</string>
</array>
```

```bash
kit/global_slot.py --status     # who holds the slot, who is waiting, current headroom
kit/global_slot.py --pause      # every heavy job skips until --resume (auto expires after 2h)
kit/global_slot.py --selftest
```

What it guarantees: at most `max_concurrent` holders machine wide. A holder whose PID died, or who held longer than `max_hold_s`, is evicted so the queue never freezes. After winning the slot a job waits up to `admit_wait_s` for swap and CPU headroom before it starts. Under pressure, low priority jobs skip their cycle and come back on their own. High priority jobs wait and take the next free slot ahead of everyone, but nothing is ever preempted.

Only gate what is heavy. Anything critical (trading, payments, the watchdogs themselves) should never call it, so it can never be blocked by it.

## Lessons

Learned the hard way, on one 8GB Mac running about 150 launchd jobs. Generic enough to apply to yours.

### launchd

1. **Agents in `~/Library/LaunchAgents` do not reliably load at login.** On one boot, 37 jobs had valid plists, correct ownership and no `Disabled` key, and none were loaded. It happened on every restart. Diff what is on disk against `launchctl list` after every boot. That is what `bootguard --fallen` does.
2. **Your guard is subject to the bug it guards against.** Run the revive check from two places, its own plist and the tick of a job that reliably survives reboots.
3. **`launchctl bootout` is not a pause.** It only removes the job from the current session. The next login reloads it from disk. A destructive job I had "paused" this way came back on its own and ran for three days. To hold a job across reboots, `launchctl disable` it AND boot it out. For anything destructive, prefer a flag file the job itself checks on startup, so no launchd state can resurrect it.
4. **Never report a job as stopped because its row vanished from `launchctl list`.** Check `launchctl print-disabled gui/$(id -u)` and say whether the state survives a reboot.
5. **A hung instance silently cancels every later run of the same label.** launchd will not start a second instance while the first is alive, and it logs nothing. One wedged run spun at 90 percent CPU on a closed pipe for 11 hours and ate four scheduled runs. Give every job a hard wall clock limit, and use `signal.alarm`, because a deadline check between work items never fires inside a spin.
6. **A live PID is not progress.** Judge health by the mtime of the artifact the job writes, never by a process being alive or a log line existing. A busy process with zero TCP sockets has lost its browser.
7. **After an unload and load cycle, verify the job actually fires.** Two jobs reloaded several times in one day sat loaded, enabled and never spawned for 17 hours. `launchctl print gui/$(id -u)/<label>` shows `runs`; make sure it climbs, or kickstart once and confirm. A wedged job looks exactly like an idle one. Wall clock `StartCalendarInterval` fired reliably where the wedged interval jobs did not.
8. **`StartCalendarInterval` with `RunAtLoad` false never makes up a slot missed while the Mac was off.** If a run matters, add `RunAtLoad` or make the job idempotent and run it more often.
9. **Set `PYTHONUNBUFFERED=1` in every plist.** Python block buffers stdout to a file, so a healthy running job writes nothing until it exits and looks dead.
10. **Read schedules from `launchctl print` or the actual plist key, never from a grep.** A grep for `StartInterval` once matched a number inside an XML comment and made a 10 minute job look like a 7 second one.
11. **Before adding a job to an auto revive roster, grep its script for `bootout`.** A job that shuts itself off by design will fight the reviver on every tick.

### Memory, disk and concurrency

12. **The root cause is usually concurrency, not leaks.** Dozens of jobs on independent timers collide. Three browser runs at about 1GB each, on top of your own browser and an AI desktop app, overcommit 8GB no matter how clean each run is. A single machine wide slot fixed what months of per script leak fixes did not.
13. **A lock without a stale breaker is a time bomb.** A dead holder or a hung holder must be evicted automatically, by PID liveness and by maximum hold time, or one crash freezes every job behind it. A lock left by a crashed run once blocked a daily job for three days.
14. **Bound the hold so one job cannot starve its siblings.** A long job holding a shared lock almost continuously once starved a sibling job for 40 hours while every status check said armed and healthy.
15. **Schedulers should skip, not queue.** A poller that finds the slot busy should exit cleanly and try on its next timer. Queueing them deep turns a busy minute into a pileup.
16. **Never wrap a job in the gate if it already takes the gate internally.** The wrapper PID holds the slot while the child PID waits for it: a deadlock until the stale breaker fires.
17. **Priority should be cooperative, not preemptive.** Letting a latency sensitive job jump the queue fixed slow response times. Killing a running holder to make room would have broken the one at a time guarantee.
18. **Gate admission on headroom, and bound the wait.** Launching a browser into a machine already at load 100 is what tips it over. But an unbounded wait for headroom on a machine that never has any deadlocks the queue.
19. **On a small Mac the swap file lives on the boot disk, so a full disk is a freeze.** Memory overcommit grows the swap file until the disk is gone. Killing processes afterward does not shrink a swap file that already grew. Freeing bytes does: old temp files, finished agent transcripts, dead session scratch directories.
20. **Use mtime as the liveness guard for cleanup.** A live session writes its scratch continuously, so an old mtime means nothing is using it. Tighten the age floor as disk gets scarcer.
21. **Hunt hidden disk eaters.** Chrome's component updater once retried a 2.8GB on device model download into the temp dir every few minutes without cleaning up. Free disk went from 11GB to under 1GB in an hour.
22. **Stop Spotlight indexing automation profiles.** Constant browser profile churn kept Spotlight reindexing at 40 to 60 percent CPU. Drop an empty `.metadata_never_index` file into each automation profile directory. No sudo needed.

### Watchdogs

23. **Every probe your watchdog runs must have a timeout.** Under swap thrash `ps`, `lsof` and `top` page fault slowly. One unbounded call froze a tick for 12 minutes, which disabled the watchdog exactly when it mattered most.
24. **A watchdog that sees zero processes is blind, not calm.** macOS `ps` has no `etimes` keyword (that is Linux). Asking for it makes `ps` print nothing, and the watchdog reported "0 flagged" every tick for days while memory climbed. Treat an empty scan as an error.
25. **Decay counters, do not reset them.** The memory pressure level flips between 1 and 2 tick to tick. Resetting a guilt counter on every clean tick meant nothing ever reached its threshold. Decay by one instead, and trigger on swap used, which is stable.
26. **Every raw number trigger needs a consequence gate.** Low free swap alone is harmless because macOS grows the swap file on demand; it only matters when disk is also low. Measure swap as a percentage of the current cap, since the cap moves. Require a danger to survive more than one tick before alerting.
27. **Load average is a bad alarm on macOS.** Spotlight, Photos analysis and the neural engine compiler routinely spike load to 25 to 40 and settle on their own.
28. **Use per kind alert cooldowns, never one global cooldown.** A false swap alarm once started a global cooldown that muted the first real disk emergency 45 minutes later.
29. **Keep a watchdog in its lane.** A machine watchdog should key only on machine death signatures. Alarming on another system's health flags produced false emergencies about problems that were not the machine's.
30. **Protect by ancestry, not by name alone.** A protected script's browser renderer never mentions the script on its command line. Walk the parent chain.
31. **Never let automation reboot the machine behind the owner's back.** An automatic emergency reboot kept firing after the owner had banned restarts. Free bytes and notify instead, and leave the reboot decision to a human.
32. **The worst incidents come from automated killers.** A blanket `pkill` on a shared browser profile took down two healthy runs at once. Kill narrowly, with grace ticks, and log every kill.

### When the Mac keeps restarting

33. **A Mac that "keeps restarting" may be panicking, not rebooting.** Check `last reboot` for the cadence, then the head of `/Library/Logs/DiagnosticReports/*.panic`. `watchdog timeout: no checkins from watchdogd` means the CPU was so saturated (load 117 on 8 cores, in our case) that the kernel's own watchdog could not be scheduled for 91 seconds. After each panic the whole herd respawns and rebuilds the load, a loop that sustains itself.
34. **Find the pegged core with `ps -Ao pcpu,rss,etime,command -r | head`.** A 4MB process at 100 percent CPU is a spin loop, not real work. In our case it was a probe launching a browser into an already thrashing machine with no timeout. A load gate on every browser launcher ended the loop.

## Good fit, and limits

**Good:** a single Mac running many unattended launchd jobs, Playwright or CDP driven Chrome, AI agents on a timer, small RAM.

**Limits:** macOS only. The reaper's Chrome rules assume Chrome for Testing, Google Chrome or `chrome-headless-shell`. Root owned system hogs cannot be killed without sudo and are deliberately left alone. A queue cannot make 8GB into 24GB; it only stops jobs from fighting over it.

## License

MIT. See [LICENSE](LICENSE).
