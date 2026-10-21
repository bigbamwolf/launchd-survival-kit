#!/usr/bin/env python3
"""REAPER: a deterministic watchdog for runaway automation processes on macOS.

Runs every few minutes from launchd. Each tick it scans the process table and
flags candidates under these rules (all configurable, all logged):

  zombie_python     orphan (ppid 1) script from stdin or a temp dir, burning CPU, no children
  idle_cdp_chrome   automation Chrome with a debugging port and zero connected clients
  orphan_headless   chrome-headless-shell reparented to launchd
  fat_renderer      automation Chrome renderer over the footprint cap (tab only, browser lives)
  fat_headless      headless shell renderer over the cap, only while swap is genuinely high
  orphan_sidecar    helper processes (e.g. MCP servers) reparented to launchd after their parent died
  idle_session      finished agent session processes, only while swap is genuinely high
  stale_tab         about:blank / new tab pages inside automation browsers, closed over CDP
  disk_reap         regenerable temp files past an age floor, only when disk is low

A flagged process must stay guilty for N consecutive ticks before it dies. Counters
decay by one on a clean tick instead of resetting, so a jittery signal cannot wipe
accumulated evidence. Anything matching protected_patterns, or descended from a
process that does, is never touched. Your own browser is structurally excluded:
only Chrome launched with an automation user-data-dir is ever considered.

Usage:
  reaper.py              one tick
  reaper.py --dry-run    log what would be killed, kill nothing
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kitconfig  # noqa: E402

DEFAULTS = {
    "disabled_rules": [],
    "grace_ticks": {
        "fat_renderer": 1, "fat_headless": 1, "zombie_python": 2,
        "idle_cdp_chrome": 2, "orphan_headless": 2, "orphan_sidecar": 2,
        "idle_session": 2, "idle_session_emergency": 1,
    },
    "automation_profile_prefixes": ["/private/tmp/", "/tmp/"],
    "zombie_script_pattern": r"(Python|python3?)\s+(-$|-\s|/tmp/|/private/tmp/)",
    "zombie_min_cpu": 20,
    "zombie_exclude_pattern": r"streamlit",
    "sidecar_pattern": "",
    "protected_patterns": [],
    "protected_ancestry_depth": 8,
    "renderer_cap_mb": 1500,
    "renderer_cap_emergency_mb": 800,
    "headless_cap_mb": 1200,
    "headless_cap_emergency_mb": 800,
    "headless_swap_gate_mb": 5500,
    "emergency_pressure_level": 4,
    "idle_session": {
        "process_pattern": "",
        "min_age_s": 600,
        "swap_gate_mb": 5000,
        "busy_fd_pattern": "",
        "kill_parent_if_same_pattern": True,
    },
    "stale_tab_urls": ["about:blank", "chrome://newtab/", "chrome://new-tab-page/"],
    "never_close_url_patterns": [],
    "disk_reap": {
        "trigger_gb": 8,
        "critical_gb": 3,
        "age_min": 360,
        "age_min_critical": 60,
        "targets": [],
    },
    "probe_timeout_s": 15,
    "ps_timeout_s": 30,
    "run_bootguard": True,
}

CFG = kitconfig.section("reaper", DEFAULTS)
STATE_DIR = kitconfig.state_dir()
STATE = os.path.join(STATE_DIR, "reaper_pending.json")
LEDGER = os.path.join(STATE_DIR, "reaper_kills.jsonl")
PROTECT_PIDS = os.path.join(STATE_DIR, "protect_pids.txt")
DRY = "--dry-run" in sys.argv


def _rx(pattern):
    return re.compile(pattern, re.I) if pattern else None


def _profile_rx():
    prefixes = [re.escape(kitconfig.expand(p)) for p in CFG["automation_profile_prefixes"]]
    if not prefixes:
        return None
    return re.compile(r"user-data-dir=(?:" + "|".join(prefixes) + ")")


AUTOMATION_PROFILE = _profile_rx()
DEBUG_PORT = re.compile(r"--remote-debugging-port=(\d+)")
ZOMBIE_SCRIPT = _rx(CFG["zombie_script_pattern"])
ZOMBIE_EXCLUDE = _rx(CFG["zombie_exclude_pattern"])
SIDECAR = _rx(CFG["sidecar_pattern"])
PROTECTED = _rx("|".join(f"(?:{p})" for p in CFG["protected_patterns"]))
NEVER_CLOSE_URL = _rx("|".join(f"(?:{p})" for p in CFG["never_close_url_patterns"]))
SESSION = _rx(CFG["idle_session"]["process_pattern"])
SESSION_BUSY_FD = CFG["idle_session"]["busy_fd_pattern"]


def enabled(rule):
    return rule not in CFG["disabled_rules"]


def _cmd_out(cmd, timeout=None):
    """Every probe is timeout bounded. Under swap thrash ps, lsof and top page fault
    slowly, and one unbounded call can freeze the whole tick for minutes, which
    disables the watchdog exactly when it is needed most."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout or CFG["probe_timeout_s"]).stdout
    except Exception:
        return ""


def disk_free_gb():
    try:
        out = subprocess.run(["df", "-g", "/"], capture_output=True, text=True).stdout
        return int(out.splitlines()[-1].split()[3])
    except Exception:
        return 99


def pressure_level():
    try:
        return int(subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True).stdout.strip())
    except Exception:
        return 1


def swap_used_mb():
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True).stdout
        m = re.search(r"used = ([0-9.]+)M", out)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


def _etime_to_secs(s):
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 86400 + h * 3600 + m * 60 + sec


def ps_all():
    """macOS ps has `etime` (formatted), NOT `etimes` (a Linux ism). Asking for
    etimes makes ps print nothing, and a watchdog that scans zero processes is
    silently blind, so an empty scan is treated as a hard error below."""
    out = _cmd_out(["ps", "axo", "pid=,ppid=,etime=,pcpu=,args="], timeout=CFG["ps_timeout_s"])
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                          "age": _etime_to_secs(parts[2]), "cpu": float(parts[3]),
                          "args": parts[4]})
        except ValueError:
            continue
    return procs


def child_procs(procs, pid):
    return [p for p in procs if p["ppid"] == pid
            and not (SIDECAR and SIDECAR.search(p["args"]))
            and "<defunct>" not in p["args"]]


def busy_fds(pid):
    if not SESSION_BUSY_FD:
        return 0
    return _cmd_out(["lsof", "-p", str(pid)]).count(SESSION_BUSY_FD)


def port_established(port):
    return "ESTABLISHED" in _cmd_out(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"])


def footprint_mb(pid):
    out = _cmd_out(["top", "-l", "1", "-pid", str(pid), "-stats", "mem"]).strip().splitlines()
    if not out:
        return 0
    m = re.match(r"([\d.]+)([KMG])", out[-1].strip())
    if not m:
        return 0
    val = float(m.group(1))
    return {"K": val / 1024, "M": val, "G": val * 1024}[m.group(2)]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f)


def log_kill(rule, pid, detail):
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rule": rule,
             "pid": pid, "detail": detail[:200], "dry": DRY}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[{entry['ts']}] {'DRY ' if DRY else ''}KILL {rule} pid={pid} {detail[:120]}")


def kill(pid):
    if DRY:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(2)
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def protected_pids():
    """PIDs listed one per line in protect_pids.txt are never killed."""
    pids = set()
    try:
        with open(PROTECT_PIDS) as f:
            for line in f:
                if line.strip().isdigit():
                    pids.add(int(line.strip()))
    except FileNotFoundError:
        pass
    return pids


def protected_by_ancestry(by_pid, pid):
    """True if this pid or any ancestor matches protected_patterns. Walking the ppid
    chain shields a protected script's browser and its renderer even though the
    renderer's own args never name the script."""
    if not PROTECTED:
        return False
    seen = set()
    cur = pid
    for _ in range(CFG["protected_ancestry_depth"]):
        p = by_pid.get(cur)
        if not p or cur in seen:
            break
        seen.add(cur)
        if PROTECTED.search(p["args"]):
            return True
        cur = p["ppid"]
    return False


def is_automation_chrome(args):
    return AUTOMATION_PROFILE is not None and bool(AUTOMATION_PROFILE.search(args))


def candidates(procs, emergency, swap_used):
    renderer_cap = CFG["renderer_cap_emergency_mb"] if emergency else CFG["renderer_cap_mb"]
    headless_cap = CFG["headless_cap_emergency_mb"] if emergency else CFG["headless_cap_mb"]
    high_swap = swap_used > CFG["headless_swap_gate_mb"]
    by_pid = {p["pid"]: p for p in procs}
    pinned = protected_pids()
    found = []
    for p in procs:
        a = p["args"]
        if p["pid"] in pinned or protected_by_ancestry(by_pid, p["pid"]):
            continue
        if "chrome-headless-shell" in a and "--type=renderer" in a:
            if enabled("fat_headless") and high_swap and footprint_mb(p["pid"]) > headless_cap:
                found.append(("fat_headless", p["pid"], a, None))
            continue
        if (enabled("zombie_python") and ZOMBIE_SCRIPT and p["ppid"] == 1
                and p["cpu"] > CFG["zombie_min_cpu"] and ZOMBIE_SCRIPT.search(a)
                and not (ZOMBIE_EXCLUDE and ZOMBIE_EXCLUDE.search(a))
                and not child_procs(procs, p["pid"])):
            found.append(("zombie_python", p["pid"], a, None))
        elif ("Contents/MacOS/Google Chrome" in a and "/Helpers/" not in a
              and is_automation_chrome(a)):
            m = DEBUG_PORT.search(a)
            if enabled("idle_cdp_chrome") and m and not port_established(int(m.group(1))):
                found.append(("idle_cdp_chrome", p["pid"], a, None))
        elif "chrome-headless-shell" in a and p["ppid"] == 1:
            if enabled("orphan_headless"):
                found.append(("orphan_headless", p["pid"], a, None))
        elif ("Chrome Helper (Renderer)" in a and "--type=renderer" in a
              and is_automation_chrome(a)):
            if enabled("fat_renderer") and footprint_mb(p["pid"]) > renderer_cap:
                found.append(("fat_renderer", p["pid"], a, None))
        elif SIDECAR and p["ppid"] == 1 and SIDECAR.search(a):
            if enabled("orphan_sidecar"):
                found.append(("orphan_sidecar", p["pid"], a, None))
    return found


def idle_session_candidates(procs, level, swap_used):
    """Finished agent sessions that never exited. Gated on swap USED, which is
    steady, rather than the pressure LEVEL, which oscillates tick to tick and kept
    wiping the counters. A session with any live child or busy file is skipped."""
    cfg = CFG["idle_session"]
    if not SESSION or not enabled("idle_session"):
        return []
    if swap_used < cfg["swap_gate_mb"] and level < CFG["emergency_pressure_level"]:
        return []
    pinned = protected_pids()
    by_pid = {p["pid"]: p for p in procs}
    found = []
    for p in procs:
        if not SESSION.search(p["args"]) or p["age"] < cfg["min_age_s"]:
            continue
        if p["pid"] in pinned or p["ppid"] in pinned:
            continue
        if protected_by_ancestry(by_pid, p["pid"]):
            continue
        if child_procs(procs, p["pid"]) or busy_fds(p["pid"]) > 0:
            continue
        parent = by_pid.get(p["ppid"])
        wrapper = p["ppid"] if (cfg["kill_parent_if_same_pattern"] and parent
                                and SESSION.search(parent["args"])) else None
        found.append(("idle_session", p["pid"], p["args"], wrapper))
    return found


def reap_stale_tabs(procs):
    if not enabled("stale_tab"):
        return
    stale_urls = set(CFG["stale_tab_urls"])
    for p in procs:
        a = p["args"]
        if "/Helpers/" in a or "Contents/MacOS/Google Chrome" not in a:
            continue
        if not is_automation_chrome(a):
            continue
        m = DEBUG_PORT.search(a)
        if not m:
            continue
        port = int(m.group(1))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                targets = json.load(r)
        except Exception:
            continue
        pages = [t for t in targets if t.get("type") == "page"]
        if len(pages) < 2:
            continue
        stale = [t for t in pages if t.get("url", "") in stale_urls
                 and not (NEVER_CLOSE_URL and NEVER_CLOSE_URL.search(t.get("url", "")))]
        if len(stale) == len(pages):
            stale = stale[1:]
        for t in stale:
            log_kill("stale_tab", p["pid"], f"port={port} close {t.get('url', '')}")
            if not DRY:
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/close/{t['id']}", timeout=3).read()
                except Exception:
                    pass


def _safe_root(path):
    path = os.path.abspath(kitconfig.expand(path))
    return path not in ("/", os.path.expanduser("~"), "/Users", "/private", "/tmp",
                        "/private/tmp", "/var", "/private/var")


def reap_disk():
    """On a small Mac the swap file lives on the boot disk, so a full disk means a
    freeze. Killing processes does not shrink a swap file that already grew; freeing
    bytes does. Deletes only what the targets list names, only past an age floor,
    and the floor tightens as disk gets scarcer. An old mtime means nothing live is
    writing there."""
    cfg = CFG["disk_reap"]
    if not enabled("disk_reap") or not cfg["targets"]:
        return
    gb = disk_free_gb()
    if gb >= cfg["trigger_gb"]:
        return
    mmin = cfg["age_min_critical"] if gb < cfg["critical_gb"] else cfg["age_min"]
    for t in cfg["targets"]:
        root = t.get("root", "")
        if not root:
            continue
        if not _safe_root(root) and (t.get("kind") == "dir" or not (t.get("path") or t.get("name"))):
            print(f"disk_reap: refusing broad target {root}, add a name or path filter")
            continue
        root = kitconfig.expand(root)
        cmd = ["find", root]
        if t.get("kind") == "dir":
            cmd += ["-mindepth", str(t.get("mindepth", 1)), "-maxdepth", str(t.get("maxdepth", 1)),
                    "-type", "d"]
        else:
            if t.get("maxdepth"):
                cmd += ["-maxdepth", str(t["maxdepth"])]
            cmd += ["-type", "f"]
        if t.get("path"):
            cmd += ["-path", t["path"]]
        if t.get("name"):
            cmd += ["-name", t["name"]]
        cmd += ["-mmin", f"+{mmin}"]
        cmd += ["-exec", "rm", "-rf", "{}", "+"] if t.get("kind") == "dir" else ["-delete"]
        if DRY:
            print("DRY disk_reap: " + " ".join(cmd))
            continue
        try:
            subprocess.run(cmd, capture_output=True, timeout=90)
        except Exception:
            pass
    after = disk_free_gb()
    if after != gb or DRY:
        log_kill("disk_reap", 0, f"disk {gb}GB -> {after}GB (age floor {mmin}min)")


def run_bootguard():
    """Second belt. Whatever loads the reaper at login is the anchor; on each tick
    it re bootstraps any roster job that fell off, bootguard's own job included."""
    if not CFG["run_bootguard"] or DRY:
        return
    try:
        subprocess.run([sys.executable, os.path.join(kitconfig.KIT_DIR, "bootguard.py")],
                       capture_output=True, timeout=60)
    except Exception:
        pass


def main():
    level = pressure_level()
    swap_used = swap_used_mb()
    emergency = level >= CFG["emergency_pressure_level"]
    procs = ps_all()
    if not procs:
        print("ERROR: process scan returned nothing, refusing to run blind", file=sys.stderr)
        return 2
    state = load_state()
    found = candidates(procs, emergency, swap_used)
    found += idle_session_candidates(procs, level, swap_used)
    grace = dict(CFG["grace_ticks"])
    if emergency:
        grace["idle_session"] = grace.get("idle_session_emergency", 1)

    flagged, new_state = set(), {}
    for rule, pid, args, wrapper in found:
        key = f"{rule}:{pid}"
        flagged.add(key)
        count = state.get(key, 0) + 1
        if count >= grace.get(rule, 2):
            log_kill(rule, pid, args)
            kill(pid)
            if wrapper:
                kill(wrapper)
        else:
            new_state[key] = count
            print(f"pending {count}/{grace.get(rule, 2)}: {key}")
    for key, count in state.items():
        if key not in flagged and count - 1 > 0:
            new_state[key] = count - 1

    reap_stale_tabs(procs)
    reap_disk()
    save_state(new_state)
    print(f"tick done, scanned={len(procs)} flagged={len(found)} pressure={level} "
          f"swap={int(swap_used)}MB disk={disk_free_gb()}GB{' EMERGENCY' if emergency else ''}")
    return 0


if __name__ == "__main__":
    rc = main()
    run_bootguard()
    sys.exit(rc)
