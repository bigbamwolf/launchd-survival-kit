#!/usr/bin/env python3
"""RAM RELIEF: hold the memory and disk line without rebooting and without killing.

Runs every 15 minutes. Each tick it:
  * measures swap used (as a percentage of the CURRENT swap cap), free disk, free memory
  * when disk is under the relief line, runs your reclaim commands (regenerable caches only)
  * notifies you when a danger survives N consecutive ticks (disk notifies on the first)

It kills nothing. Automated killers are where most self inflicted incidents come
from, so this is the layer that frees bytes and tells you, and leaves the judgment
call to you.

Usage:
  ram_relief.py
  ram_relief.py --test-notify   send a test notification through notify_command
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kitconfig  # noqa: E402

DEFAULTS = {
    "swap_warn_pct": 88,
    "disk_relief_gb": 14,
    "disk_warn_gb": 10,
    "freemem_warn_pct": 8,
    "sustain_ticks": 2,
    "alert_cooldown_s": 7200,
    "reclaim_commands": [],
    "notify_command": "",
}

CFG = kitconfig.section("ram_relief", DEFAULTS)
STATE_DIR = kitconfig.state_dir()
LOG = os.path.join(STATE_DIR, "ram_relief.log")
STATE = os.path.join(STATE_DIR, "ram_relief_state.json")


def sh(cmd, env=None):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=120, env=env).stdout.strip()
    except Exception:
        return ""


def swap():
    """(used MB, total MB). macOS grows the swap cap on demand, so an absolute MB
    threshold ages out; compare against the current cap instead."""
    raw = sh("sysctl -n vm.swapusage")
    try:
        used = float(raw.split("used =")[1].split("M")[0].strip())
        total = float(raw.split("total =")[1].split("M")[0].strip())
        return used, total
    except Exception:
        return 0.0, 0.0


def disk_free_gb():
    raw = sh("df -g / | tail -1")
    try:
        return int(raw.split()[3])
    except Exception:
        return 99


def free_pct():
    raw = sh("memory_pressure | tail -3")
    for line in raw.splitlines():
        if "free percentage" in line:
            try:
                return int(line.split(":")[1].strip().rstrip("%"))
            except Exception:
                return 100
    return 100


def reclaim():
    before = disk_free_gb()
    for cmd in CFG["reclaim_commands"]:
        sh(cmd)
    after = disk_free_gb()
    return ([f"reclaimed {after - before}GB"] if after > before else []), after


def notify(text):
    cmd = CFG["notify_command"]
    if not cmd:
        return False
    env = dict(os.environ, LSK_MESSAGE=text)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30, env=env)
        return r.returncode == 0
    except Exception:
        return False


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    if "--test-notify" in sys.argv:
        ok = notify("launchd survival kit: test notification")
        print("notify ok" if ok else "notify failed or notify_command is empty")
        return 0 if ok else 1

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sw, sw_total = swap()
    sw_pct = (sw / sw_total * 100) if sw_total else 0
    disk = disk_free_gb()
    pct = free_pct()
    notes = []

    if disk < CFG["disk_relief_gb"]:
        freed, disk = reclaim()
        notes.extend(freed)

    kinds, danger = set(), []
    if sw_pct > CFG["swap_warn_pct"]:
        kinds.add("swap")
        danger.append(f"swap {sw:.0f}MB of {sw_total:.0f}MB cap, {sw_pct:.0f} pct")
    if disk < CFG["disk_warn_gb"]:
        kinds.add("disk")
        danger.append(f"disk {disk}GB under {CFG['disk_warn_gb']}GB")
    if pct < CFG["freemem_warn_pct"]:
        kinds.add("mem")
        danger.append(f"free mem {pct} pct under {CFG['freemem_warn_pct']} pct")

    line = (f"{ts} swap={sw:.0f}/{sw_total:.0f}MB ({sw_pct:.0f}%) disk={disk}GB "
            f"freemem={pct}% ") + ("DANGER: " + "; ".join(danger) if danger else "ok")
    if notes:
        line += " | " + "; ".join(notes)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")

    state = load_state()
    streak = state.get("danger_streak", 0) + 1 if danger else 0
    state["danger_streak"] = streak

    # Cooldowns are per KIND. One global cooldown started by a harmless swap spike
    # can swallow the first genuine disk emergency right after it. Disk does not
    # self heal the way a swap spike does, so it fires on the first tick.
    cools = state.get("cooldowns", {})
    now = time.time()
    due = [k for k in kinds
           if now - cools.get(k, 0) > CFG["alert_cooldown_s"]
           and (k == "disk" or streak >= CFG["sustain_ticks"])]

    if due:
        body = (f"[RAM RELIEF] {ts}, sustained {streak} ticks, firing on: "
                f"{', '.join(sorted(due))}. " + "; ".join(danger)
                + ". Nothing was killed and nothing was restarted.")
        if notify(body):
            for k in due:
                cools[k] = now

    state.update({"cooldowns": cools, "last_tick": ts, "swap": sw, "swap_total": sw_total,
                  "swap_pct": round(sw_pct, 1), "disk": disk, "freemem": pct})
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
