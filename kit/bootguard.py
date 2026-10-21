#!/usr/bin/env python3
"""BOOTGUARD: revive launchd agents that silently failed to load.

macOS does not reliably load every plist in ~/Library/LaunchAgents at login.
Jobs sit on disk with a clean plutil lint, correct ownership and no Disabled key,
yet `launchctl list` does not show them. BOOTGUARD diffs your revive roster
against what launchd actually has loaded and bootstraps anything that fell off.

It touches ONLY labels on the roster, so a job you killed on purpose stays dead.

Usage:
  bootguard.py              reconcile the roster now
  bootguard.py --dry-run    show what would be revived, change nothing
  bootguard.py --fallen     list EVERY plist on disk that is not loaded (diagnostic)
"""
import datetime
import json
import os
import plistlib
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kitconfig  # noqa: E402

DEFAULTS = {
    "launch_agents_dir": "~/Library/LaunchAgents",
    "revive": [],
    "respect_disabled": True,
    "skip_self_terminating": True,
    "interpreters": ["python3", "python", "bash", "zsh", "sh", "env", "caffeinate"],
}

CFG = kitconfig.section("bootguard", DEFAULTS)
LA = kitconfig.expand(CFG["launch_agents_dir"])
UID = os.getuid()
DRY = "--dry-run" in sys.argv


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def loaded_labels():
    out = run(["launchctl", "list"]).stdout
    return {ln.split("\t")[-1].strip() for ln in out.splitlines()[1:] if ln.strip()}


def disabled_labels():
    out = run(["launchctl", "print-disabled", f"gui/{UID}"]).stdout
    found = set()
    for m in re.finditer(r'"([^"]+)"\s*=>\s*(disabled|true)', out):
        found.add(m.group(1))
    return found


def program_args(plist_path):
    try:
        with open(plist_path, "rb") as f:
            d = plistlib.load(f)
    except Exception:
        return None
    return [str(a) for a in (d.get("ProgramArguments") or
                             ([d["Program"]] if d.get("Program") else []))]


def target_script(args):
    """First absolute path argument that is not a bare interpreter."""
    for a in args:
        if a.startswith("/") and a.rsplit("/", 1)[-1] not in CFG["interpreters"]:
            return a
    return None


def self_terminating(script):
    """A job that boots itself out by design must never be on the roster, or the
    guard fights its shutdown logic on every tick."""
    if os.path.abspath(script).startswith(kitconfig.KIT_DIR + os.sep):
        return False
    try:
        with open(script, errors="ignore") as f:
            text = f.read()
        return "launchctl bootout" in text or '"bootout"' in text
    except Exception:
        return False


def bootstrap(plist):
    return run(["launchctl", "bootstrap", f"gui/{UID}", plist])


def list_fallen():
    live = loaded_labels()
    on_disk = sorted(f[:-6] for f in os.listdir(LA) if f.endswith(".plist"))
    fallen = [lbl for lbl in on_disk if lbl not in live]
    roster = set(CFG["revive"])
    for lbl in fallen:
        print(f"{'ROSTER ' if lbl in roster else '       '}{lbl}")
    print(f"{len(fallen)} of {len(on_disk)} plists on disk are not loaded")
    return 0


def main():
    if "--fallen" in sys.argv:
        return list_fallen()

    live = loaded_labels()
    disabled = disabled_labels() if CFG["respect_disabled"] else set()
    revived, skipped, failed = [], [], []

    for label in CFG["revive"]:
        plist = os.path.join(LA, label + ".plist")
        if label in live:
            continue
        if not os.path.exists(plist):
            skipped.append((label, "no plist"))
            continue
        if label in disabled:
            skipped.append((label, "disabled in launchd, run launchctl enable to re arm"))
            continue
        args = program_args(plist)
        if args is None:
            skipped.append((label, "unreadable plist"))
            continue
        script = target_script(args)
        if script and not os.path.exists(script):
            skipped.append((label, f"target missing {script}"))
            continue
        if CFG["skip_self_terminating"] and script and self_terminating(script):
            skipped.append((label, "self terminating job, calls launchctl bootout"))
            continue
        if DRY:
            revived.append(label + " (dry run)")
            continue
        r = bootstrap(plist)
        if r.returncode == 0:
            revived.append(label)
            continue
        if not CFG["respect_disabled"]:
            run(["launchctl", "enable", f"gui/{UID}/{label}"])
            r = bootstrap(plist)
            if r.returncode == 0:
                revived.append(label)
                continue
        failed.append((label, (r.stderr or "").strip()[:160]))

    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "revived": revived,
        "skipped": skipped,
        "failed": failed,
        "roster": len(CFG["revive"]),
        "dry": DRY,
    }
    with open(os.path.join(kitconfig.state_dir(), "bootguard.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
