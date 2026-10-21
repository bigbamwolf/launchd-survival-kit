#!/usr/bin/env python3
"""
GLOBAL SLOT: a machine wide, one at a time queue for heavy jobs.

THE PROBLEM: dozens of launchd jobs fire on independent timers, so two or three
browser heavy tasks can launch Chrome at the SAME instant. On an 8GB Mac that
blows memory, starves watchdogd, and can end in a kernel panic. Per project
locks do not help when the collision is between projects.

THE FIX: one machine wide token. Every heavy job wraps its work in `heavy()`;
only MAX_CONCURRENT run at once (default 1), the rest WAIT their turn. This is
passive, deterministic infrastructure: no daemon, nothing to keep alive, it
survives reboots because it is just a lock file with rules.

Design, with the scars baked in:
  * ADMISSION CONTROL: after winning the slot, wait (bounded) for swap and CPU
    headroom before launching, so a browser never starts into a storm. Bounded so
    a tight baseline can never deadlock the queue.
  * STALE BREAK: a lock from a dead PID (crashed job) or held longer than MAX_HOLD
    (hung job) is stolen, so the queue never freezes.
  * BOUNDED HOLD: MAX_HOLD caps how long one holder blocks the rest, so no job can
    starve its siblings for hours.
  * OPT IN: only heavy jobs call heavy(). Anything critical (trading, payments, the
    watchdogs themselves) simply never calls it and is never gated.
  * SCHEDULERS SKIP, they do not pile up: wait=False raises SlotBusy after
    QUEUE_GRACE so a poller re fires on its next timer instead of queueing deep.
  * PRIORITY LANE: priority="hi" waits instead of skipping and jumps the queue. A
    LOW job will not grab a freshly freed slot while a HIGH job is waiting for it.
    Cooperative, NOT preemptive: a live holder is never killed.
  * PRESSURE VALVE: under RAM or CPU pressure, LOW jobs skip their cycle and resume
    on their own once headroom returns. HIGH jobs keep running.
  * PAUSE FLAG: touch <state_dir>/HEAVY_PAUSE to pause every heavy job. It expires
    on its own after PAUSE_MAX_AGE so a forgotten pause cannot leave the fleet dark.

Usage (Python):
    import sys; sys.path.insert(0, "/path/to/launchd-survival-kit/kit")
    import global_slot as G
    with G.heavy("my-scraper"):                       # blocks until it is this job's turn
        ... launch browser, do the run, close browser ...

    with G.heavy("chat-reply", priority="hi", timeout=270):   # jumps the queue
        ...

    try:                                               # a scheduled poller that skips
        with G.heavy("poller", wait=False):
            ...
    except G.SlotBusy:
        sys.exit(0)

Usage (any command, no code changes): wrap it with the runner
    with_global_slot.py [--skip-if-busy] [--priority hi] [--timeout N] <label> <cmd> [args...]

CLI:
    global_slot.py --status | --selftest | --pause | --resume
"""
import contextlib
import errno
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kitconfig  # noqa: E402

_CFG = kitconfig.section("global_slot", {
    "max_concurrent": 1,
    "max_hold_s": 1800,
    "queue_grace_s": 120,
    "admit_wait_s": 300,
    "min_swap_free_mb": 1024,
    "max_load1": 6.0,
    "hi_wait_fresh_s": 15,
    "pause_max_age_s": 7200,
})

STATE_DIR = kitconfig.state_dir()
LOCK = os.path.join(STATE_DIR, "global_heavy.lock")
LOG = os.path.join(STATE_DIR, "global_heavy.log")
PAUSE_FLAG = os.path.join(STATE_DIR, "HEAVY_PAUSE")
HI_WAIT = os.path.join(STATE_DIR, "global_heavy.hiwait")

MAX_CONCURRENT = int(_CFG["max_concurrent"])
MAX_HOLD = int(_CFG["max_hold_s"])
QUEUE_GRACE = int(_CFG["queue_grace_s"])
ADMIT_WAIT = int(_CFG["admit_wait_s"])
MIN_SWAP_FREE_MB = float(_CFG["min_swap_free_mb"])
MAX_LOAD1 = float(_CFG["max_load1"])
HI_WAIT_FRESH = int(_CFG["hi_wait_fresh_s"])
PAUSE_MAX_AGE = int(_CFG["pause_max_age_s"])


class SlotBusy(Exception):
    """The gate is full of live, fresh holders and the caller asked not to wait.
    A SCHEDULED job catches this and exits 0 -- it re-fires on its next timer."""


def _swap_free_mb():
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"free = ([0-9.]+)M", out)
        return float(m.group(1)) if m else 9999.0
    except Exception:
        return 9999.0


def _load1():
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def _headroom_ok():
    """Safe to launch heavy work right now? Swap has room AND CPU is not saturated."""
    return _swap_free_mb() >= MIN_SWAP_FREE_MB and _load1() < MAX_LOAD1


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM   # exists but owned by another user


def _log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _read_holders():
    """Live holders as a list of (pid, ts, label, priority). Prunes dead/expired."""
    try:
        with open(LOCK) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []
    now = time.time()
    live = []
    for ln in lines:
        parts = ln.split(",", 3)
        try:
            pid, ts = int(parts[0]), float(parts[1])
        except Exception:
            continue
        label = parts[2] if len(parts) > 2 else ""
        priority = parts[3] if len(parts) > 3 else "lo"
        if not _pid_alive(pid):
            continue                       # crashed holder, drop
        if (now - ts) > MAX_HOLD:
            _log(f"stale-break: dropping hung holder pid={pid} label={label} "
                 f"age={int(now - ts)}s")
            continue                       # hung holder, steal
        live.append((pid, ts, label, priority))
    return live


def _write_holders(holders):
    tmp = LOCK + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        for h in holders:
            pid, ts, label = h[0], h[1], h[2]
            priority = h[3] if len(h) > 3 else "lo"
            f.write(f"{pid},{ts},{label},{priority}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LOCK)


# A coarse cross-process guard around the read-modify-write of the holders file,
# so two jobs claiming at the same instant do not both think they won the slot.
_MX = os.path.join(STATE_DIR, "global_heavy.mx")


@contextlib.contextmanager
def _mutex(timeout=30):
    os.makedirs(STATE_DIR, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(_MX, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            # break a stale mutex (holder died mid-critical-section)
            try:
                if (time.time() - os.path.getmtime(_MX)) > 30:
                    os.remove(_MX)
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                # give up the mutex rather than hang forever; worst case a rare race
                try: os.remove(_MX)
                except OSError: pass
                continue
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.close(fd)
            os.remove(_MX)
        except Exception:
            pass


def _hi_waiting_others():
    """True if a HIGH-priority job OTHER than us is currently (freshly) waiting for
    the slot. LOCK-FREE read so it is safe to call from inside _mutex (no reentry)."""
    try:
        with open(HI_WAIT) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return False
    now = time.time()
    me = os.getpid()
    for ln in lines:
        parts = ln.split(",", 1)
        try:
            pid, ts = int(parts[0]), float(parts[1])
        except Exception:
            continue
        if pid == me or not _pid_alive(pid):
            continue
        if (now - ts) <= HI_WAIT_FRESH:
            return True
    return False


def _hi_wait_set(present):
    """Register (present=True) or clear (False) our pid as a waiting HIGH job,
    pruning stale/dead entries. Refresh (set True again each poll) keeps us live."""
    with _mutex():
        now = time.time()
        me = os.getpid()
        keep = []
        try:
            with open(HI_WAIT) as f:
                for ln in f:
                    p = ln.strip().split(",", 1)
                    try:
                        pid, ts = int(p[0]), float(p[1])
                    except Exception:
                        continue
                    if pid == me or not _pid_alive(pid):
                        continue
                    if (now - ts) > HI_WAIT_FRESH:
                        continue
                    keep.append((pid, ts))
        except FileNotFoundError:
            pass
        if present:
            keep.append((me, now))
        tmp = HI_WAIT + f".tmp.{me}"
        with open(tmp, "w") as f:
            for pid, ts in keep:
                f.write(f"{pid},{ts}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HI_WAIT)


def _try_claim(label, priority="lo"):
    """Atomically take a slot if fewer than MAX_CONCURRENT live holders. True on win.
    A LOW job will NOT take a freshly-freed slot while a HIGH job is waiting for it,
    so HIGH jumps the queue the moment the current holder finishes."""
    with _mutex():
        holders = _read_holders()
        holders = [h for h in holders if h[0] != os.getpid()]   # drop any of ours
        if len(holders) >= MAX_CONCURRENT:
            return False
        if priority != "hi" and _hi_waiting_others():
            return False                   # yield this free slot to a waiting HIGH job
        holders.append((os.getpid(), time.time(), label, priority))
        _write_holders(holders)
        return True


def _release():
    with _mutex():
        holders = [h for h in _read_holders() if h[0] != os.getpid()]
        _write_holders(holders)


def acquire(label="heavy", wait=True, timeout=1800, poll=3, priority="lo"):
    """Manual acquire for callers whose launch and close are in different scopes
    (e.g. a launch_browser() / close_browser() pair). Blocks until this process holds
    a slot, or raises SlotBusy if wait=False and the slot stays busy past QUEUE_GRACE,
    or if wait=True and `timeout` expires first. The SAME process must later call
    release(). priority="hi" jumps the queue (see PRIORITY LANE in the module docstring)."""
    if os.path.exists(PAUSE_FLAG):
        try:
            paused_age = time.time() - os.path.getmtime(PAUSE_FLAG)
        except Exception:
            paused_age = 0
        if paused_age < PAUSE_MAX_AGE:
            _log(f"PAUSED label={label} (HEAVY_PAUSE present, skipping)")
            raise SlotBusy(f"heavy gate paused; {label} skipping this cycle")
        try:
            os.remove(PAUSE_FLAG)
            _log("HEAVY_PAUSE expired, auto resumed")
        except Exception:
            pass
    if priority != "hi" and not _headroom_ok():
        _log(f"SUSPENDED label={label} (low priority, no headroom swap_free={int(_swap_free_mb())}MB "
             f"load={_load1():.1f}; resumes on its own when headroom returns)")
        raise SlotBusy(f"{label} suspended under memory pressure; resumes when headroom returns")
    deadline = time.time() + (timeout if wait else QUEUE_GRACE)
    got = _try_claim(label, priority)
    try:
        while not got and time.time() < deadline:
            if priority == "hi":
                _hi_wait_set(True)         # signal LOW jobs to yield the slot to us
            time.sleep(poll)
            got = _try_claim(label, priority)
    finally:
        if priority == "hi":
            _hi_wait_set(False)
    if not got:
        _log(f"SKIP label={label} (slot busy, grace expired)")
        raise SlotBusy(f"global heavy slot busy; {label} skipping this cycle")
    # hold the slot, then wait (bounded) for real headroom before the heavy work
    admit_deadline = time.time() + ADMIT_WAIT
    while not _headroom_ok() and time.time() < admit_deadline:
        _log(f"WAIT-HEADROOM label={label} swap_free={int(_swap_free_mb())}MB "
             f"load={_load1():.1f}")
        time.sleep(poll)
    _log(f"HOLD label={label} prio={priority} swap_free={int(_swap_free_mb())}MB "
         f"load={_load1():.1f}")
    return True


def release(label="heavy"):
    _release()
    _log(f"RELEASE label={label}")


@contextlib.contextmanager
def heavy(label="heavy", wait=True, timeout=1800, poll=3, priority="lo"):
    """Hold a global heavy-task slot for the wrapped work. Wrap the ENTIRE heavy
    lifetime (browser launch through close). Queues one-at-a-time by default."""
    acquire(label, wait=wait, timeout=timeout, poll=poll, priority=priority)
    try:
        yield
    finally:
        release(label)


def _selftest():
    ok = True
    # Shrink the waits so the test runs in seconds instead of minutes (the real
    # QUEUE_GRACE/ADMIT_WAIT are for live paced jobs, not the test).
    global QUEUE_GRACE, ADMIT_WAIT, STATE_DIR, LOCK, LOG, PAUSE_FLAG, HI_WAIT, _MX
    QUEUE_GRACE, ADMIT_WAIT = 2, 0
    import tempfile
    STATE_DIR = tempfile.mkdtemp(prefix="global_slot_selftest_")
    LOCK = os.path.join(STATE_DIR, "global_heavy.lock")
    LOG = os.path.join(STATE_DIR, "global_heavy.log")
    PAUSE_FLAG = os.path.join(STATE_DIR, "HEAVY_PAUSE")
    HI_WAIT = os.path.join(STATE_DIR, "global_heavy.hiwait")
    _MX = os.path.join(STATE_DIR, "global_heavy.mx")
    # clean slate
    for p in (LOCK, _MX, HI_WAIT):
        try: os.remove(p)
        except FileNotFoundError: pass

    # 1. single claim works, recorded as one live holder
    assert _try_claim("A"), "first claim should win"
    with _mutex():
        h = _read_holders()
    assert len(h) == 1 and h[0][2] == "A" and h[0][3] == "lo", f"expected 1 holder A/lo, got {h}"
    print("[ OK ] claim records one live holder with priority")

    # 2. a non-waiting claim while full raises SlotBusy. Fake full with an alive pid.
    ppid = os.getppid()
    with _mutex():
        _write_holders([(ppid, time.time(), "fake-live", "lo")])
    try:
        with heavy("B", wait=False):
            ok = False; print("[FAIL] should have raised SlotBusy while full")
    except SlotBusy:
        print("[ OK ] wait=False raises SlotBusy when full")

    # 3. stale-break: a holder older than MAX_HOLD is dropped
    with _mutex():
        _write_holders([(ppid, time.time() - MAX_HOLD - 10, "hung", "lo")])
    with _mutex():
        h = _read_holders()
    assert h == [], f"stale holder should be pruned, got {h}"
    print("[ OK ] stale-break drops a hung holder")

    # 4. dead-pid holder is pruned
    with _mutex():
        _write_holders([(999999, time.time(), "dead", "lo")])
    with _mutex():
        h = _read_holders()
    assert h == [], f"dead-pid holder should be pruned, got {h}"
    print("[ OK ] dead-pid holder pruned")

    # 5. PRIORITY: a LOW job will not claim a free slot while a HIGH job is waiting.
    for p in (LOCK, HI_WAIT):
        try: os.remove(p)
        except FileNotFoundError: pass
    # simulate a live HIGH waiter that is not us (our parent is alive)
    with _mutex():
        tmp = HI_WAIT + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(f"{ppid},{time.time()}\n")
        os.replace(tmp, HI_WAIT)
    assert _hi_waiting_others(), "should see the HIGH waiter"
    assert not _try_claim("low-yield", priority="lo"), "LOW must yield to a waiting HIGH"
    assert _try_claim("hi-wins", priority="hi"), "HIGH must still be able to claim"
    print("[ OK ] LOW yields a free slot to a waiting HIGH, HIGH still claims")
    _release()
    # stale HIGH waiter (old ts) no longer blocks LOW
    with _mutex():
        tmp = HI_WAIT + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(f"{ppid},{time.time() - HI_WAIT_FRESH - 5}\n")
        os.replace(tmp, HI_WAIT)
    assert not _hi_waiting_others(), "a stale HIGH waiter must be ignored"
    assert _try_claim("low-ok", priority="lo"), "LOW claims when no fresh HIGH waits"
    print("[ OK ] a stale HIGH waiter does not starve LOW")
    _release()

    # 6. real acquire/release round-trip
    for p in (LOCK, HI_WAIT):
        try: os.remove(p)
        except FileNotFoundError: pass
    with heavy("C", wait=True, timeout=10, priority="hi"):
        with _mutex():
            h = _read_holders()
        assert any(x[2] == "C" and x[3] == "hi" for x in h), "C/hi should hold during the block"
    with _mutex():
        h = _read_holders()
    assert not any(x[2] == "C" for x in h), "C should be released after the block"
    print("[ OK ] acquire/release round-trip (priority)")

    # cleanup
    for p in (LOCK, _MX, HI_WAIT):
        try: os.remove(p)
        except FileNotFoundError: pass
    import shutil
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    print("[ OK ] selftest passed" if ok else "[FAIL] selftest had failures")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--pause" in sys.argv:
        open(PAUSE_FLAG, "w").close()
        print(f"paused, expires in {PAUSE_MAX_AGE}s: {PAUSE_FLAG}")
        sys.exit(0)
    if "--resume" in sys.argv:
        try:
            os.remove(PAUSE_FLAG)
        except FileNotFoundError:
            pass
        print("resumed")
        sys.exit(0)
    if "--status" in sys.argv:
        with _mutex():
            for pid, ts, label, priority in _read_holders():
                print(f"HOLDING pid={pid} age={int(time.time()-ts)}s label={label} prio={priority}")
        hw = []
        try:
            now = time.time()
            with open(HI_WAIT) as f:
                for ln in f:
                    p = ln.strip().split(",", 1)
                    if len(p) == 2 and _pid_alive(int(p[0])) and (now - float(p[1])) <= HI_WAIT_FRESH:
                        hw.append(p[0])
        except Exception:
            pass
        if hw:
            print(f"HIGH waiting: {', '.join(hw)}")
        print(f"swap_free={int(_swap_free_mb())}MB load={_load1():.1f} "
              f"headroom_ok={_headroom_ok()}")
        sys.exit(0)
    print("usage: global_slot.py --status | --selftest | --pause | --resume")
