#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: ./install.sh [--load] [--unload] [--prefix com.yourname.lsk]

  (no flag)   copy the example config and render plists into ./build, change nothing else
  --load      also copy the rendered plists into ~/Library/LaunchAgents and bootstrap them
  --unload    boot out and DISABLE the kit's jobs (disable survives a reboot, bootout alone does not)
  --prefix    launchd label prefix, default com.example.lsk (or $LSK_LABEL_PREFIX)
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${LSK_LABEL_PREFIX:-com.example.lsk}"
ACTION="render"

while [ $# -gt 0 ]; do
  case "$1" in
    --load) ACTION="load" ;;
    --unload) ACTION="unload" ;;
    --prefix) PREFIX="${2:?--prefix needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done

[ "$(uname -s)" = "Darwin" ] || { echo "macOS only (launchd)"; exit 1; }
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "python3 not found"; exit 1; }

KIT="${ROOT}/kit"
CONFIG_DIR="${HOME}/.config/launchd-survival-kit"
STATE_DIR="${HOME}/.local/state/launchd-survival-kit"
LOGS="${STATE_DIR}/logs"
BUILD="${ROOT}/build"
LA="${HOME}/Library/LaunchAgents"
UID_NUM="$(id -u)"
JOBS="bootguard reaper ram-relief"

if [ "$ACTION" = "unload" ]; then
  for j in $JOBS; do
    label="${PREFIX}.${j}"
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
    launchctl disable "gui/${UID_NUM}/${label}" 2>/dev/null || true
    echo "unloaded + disabled: ${label}"
  done
  echo "re arm later with: launchctl enable gui/${UID_NUM}/<label> && launchctl bootstrap gui/${UID_NUM} ${LA}/<label>.plist"
  exit 0
fi

mkdir -p "$CONFIG_DIR" "$LOGS" "$BUILD"
chmod +x "${KIT}"/*.py

if [ ! -f "${CONFIG_DIR}/config.json" ]; then
  sed "s|__PREFIX__|${PREFIX}|g" "${ROOT}/config.example.json" > "${CONFIG_DIR}/config.json"
  echo "config:  ${CONFIG_DIR}/config.json (edit the revive roster and patterns)"
else
  echo "config:  ${CONFIG_DIR}/config.json (kept existing)"
fi

render() {
  sed -e "s|__PREFIX__|${PREFIX}|g" \
      -e "s|__PYTHON__|${PYTHON}|g" \
      -e "s|__KIT__|${KIT}|g" \
      -e "s|__LOGS__|${LOGS}|g" \
      -e "s|__HOME__|${HOME}|g" \
      "$1" > "$2"
  plutil -lint "$2" >/dev/null
}

for f in "${ROOT}"/launchd/*.plist; do
  name="$(basename "$f" .plist)"
  render "$f" "${BUILD}/${PREFIX}.${name}.plist"
done
echo "plists: rendered into ${BUILD}"

"$PYTHON" "${KIT}/global_slot.py" --selftest >/dev/null && echo "global_slot selftest: ok"

if [ "$ACTION" = "load" ]; then
  mkdir -p "$LA"
  for j in $JOBS; do
    label="${PREFIX}.${j}"
    cp "${BUILD}/${label}.plist" "${LA}/${label}.plist"
    launchctl enable "gui/${UID_NUM}/${label}" 2>/dev/null || true
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_NUM}" "${LA}/${label}.plist"
    echo "loaded:  ${label}"
  done
  echo "verify:  launchctl print gui/${UID_NUM}/${PREFIX}.reaper | grep -E 'state|runs'"
else
  echo "dry install done. Review ${BUILD}, then run ./install.sh --load"
fi
echo "the example wrapped job plist is a template only, it is never loaded"
