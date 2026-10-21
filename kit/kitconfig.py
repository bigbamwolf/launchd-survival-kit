"""Shared config loader for launchd survival kit.

Lookup order for the config file:
  1. $LSK_CONFIG
  2. ~/.config/launchd-survival-kit/config.json
  3. config.json next to this kit (repo root)

Every component works with no config file at all, using built in defaults.
Paths in the config may use ~ and $VARS.
"""
import json
import os

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(KIT_DIR)


def expand(path):
    return os.path.expanduser(os.path.expandvars(str(path)))


def config_path():
    candidates = [
        os.environ.get("LSK_CONFIG", ""),
        expand("~/.config/launchd-survival-kit/config.json"),
        os.path.join(REPO_DIR, "config.json"),
    ]
    for c in candidates:
        if c and os.path.isfile(expand(c)):
            return expand(c)
    return None


def _load_all():
    path = config_path()
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


def section(name, defaults):
    """Return defaults overlaid with the named config section (one level deep merge)."""
    raw = _load_all().get(name, {}) or {}
    out = dict(defaults)
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update({kk: vv for kk, vv in v.items() if not kk.startswith("_")})
            out[k] = merged
        else:
            out[k] = v
    return out


def state_dir():
    d = os.environ.get("LSK_STATE_DIR") or _load_all().get(
        "state_dir", "~/.local/state/launchd-survival-kit")
    d = expand(d)
    os.makedirs(d, exist_ok=True)
    return d
