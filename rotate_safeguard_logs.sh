#!/usr/bin/env bash
# Rotate safeguard logs nightly at 01:30.
#
# Moves the day's working logs into a dated archive, keeps 30 days, and
# clears the working logs so the web Activity Log starts fresh each morning.
#
# Logs rotated: doctor actions restore safeguard-watchdog telegram
# Archive dir:  ~/.hermes/safeguard/archives/<YYYY-MM-DD>/
# Retention:    30 days (older archives pruned)
set -euo pipefail

SAFE="${HOME:-/home/blackieserver}/.hermes/safeguard"
ARCH="${SAFE}/archives"
RETENTION_DAYS=30
LOGS=(doctor actions restore safeguard-watchdog telegram)

mkdir -p "${ARCH}"
DAY="$(date +%Y-%m-%d)"
DEST="${ARCH}/${DAY}"
mkdir -p "${DEST}"

moved=0
for name in "${LOGS[@]}"; do
    src="${SAFE}/${name}.log"
    [ -f "${src}" ] || continue
    # Keep the tail of each log in the archive (last 2000 lines) so a
    # full-day dump doesn't balloon the archive, while preserving the
    # recent history the operator will want to eyeball.
    tail -n 2000 "${src}" > "${DEST}/${name}.log"
    : > "${src}"
    moved=$((moved + 1))
done

# Prune archives older than retention window.
find "${ARCH}" -maxdepth 1 -type d -name '20*' -printf '%f\n' 2>/dev/null \
  | sort \
  | head -n -"${RETENTION_DAYS}" \
  | while read -r d; do [ -n "${d}" ] && rm -rf "${ARCH}/${d}"; done

echo "$(date '+%Y-%m-%d %H:%M:%S') safeguard log rotation: moved ${moved} logs, retention ${RETENTION_DAYS}d"
