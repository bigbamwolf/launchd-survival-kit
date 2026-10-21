#!/usr/bin/env python3
"""
Run any command while holding the global heavy slot (global_slot.py).

The one line way to put a browser or heavy job behind the queue WITHOUT editing the
job's code: wrap it at the launchd level. This process holds the slot for the whole
lifetime of the child and releases it when the child exits.

In a launchd plist, replace ProgramArguments with:
    /usr/bin/python3 /path/to/kit/with_global_slot.py <label> <original program> <args...>

Flags (must come before <label>):
    --skip-if-busy   for SCHEDULERS: if the slot stays busy past the grace window,
                     skip this cycle (exit 0) instead of queueing. The job re fires on
                     its next timer. Without this flag it WAITS its turn.
    --priority hi    latency sensitive job: WAIT and jump the queue ahead of background
                     jobs. Pair with --timeout so a stuck slot cannot pin the launchd
                     instance (a hung instance blocks every later slot of its label).
    --timeout N      cap the wait at N seconds, then skip this cycle (exit 0).

Never wrap a job that already acquires the slot internally. The wrapper PID would hold
the slot while the child PID waits for it, a deadlock until the stale break fires.

Examples:
    with_global_slot.py nightly-scrape /usr/bin/python3 scrape.py
    with_global_slot.py --skip-if-busy poller /usr/bin/python3 poll.py
    with_global_slot.py --priority hi --timeout 270 chat-reply /bin/bash reply.sh
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import global_slot as G  # noqa: E402


def main():
    argv = sys.argv[1:]
    skip_if_busy = False
    priority = "lo"
    timeout = None
    while argv and argv[0].startswith("--"):
        flag = argv[0]
        if flag == "--skip-if-busy":
            skip_if_busy = True
            argv = argv[1:]
        elif flag == "--priority":
            priority = argv[1] if len(argv) > 1 else "lo"
            argv = argv[2:]
        elif flag == "--timeout":
            try:
                timeout = int(argv[1])
            except (IndexError, ValueError):
                print("with_global_slot: --timeout needs an integer", file=sys.stderr)
                return 2
            argv = argv[2:]
        else:
            break

    if len(argv) < 2:
        print("usage: with_global_slot.py [--skip-if-busy] [--priority hi] [--timeout N] "
              "<label> <command> [args...]", file=sys.stderr)
        return 2

    label, cmd = argv[0], argv[1:]
    acq = {"wait": not skip_if_busy, "priority": priority}
    if timeout is not None:
        acq["timeout"] = timeout

    try:
        G.acquire(label, **acq)
    except G.SlotBusy as e:
        print(f"[with_global_slot] {label}: {e}", flush=True)
        return 0

    try:
        return subprocess.run(cmd).returncode
    finally:
        G.release(label)


if __name__ == "__main__":
    sys.exit(main())
