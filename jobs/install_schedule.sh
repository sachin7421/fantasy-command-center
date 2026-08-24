#!/usr/bin/env bash
# Install the Fantasy Command Center cron jobs (macOS / Linux).
#
# Usage:
#   bash jobs/install_schedule.sh            # install
#   bash jobs/install_schedule.sh --remove   # uninstall
#   bash jobs/install_schedule.sh --dry-run  # print the crontab without writing
#
# Every job is idempotent, so a missed run is not a problem: the next run
# recomputes from current state.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
CLI="$PROJECT_ROOT/fcc.py"
LOG_DIR="$PROJECT_ROOT/logs"
MARKER="# fantasy-command-center"

if [ ! -x "$PYTHON" ]; then
  echo "Virtualenv not found at $PYTHON" >&2
  echo "Create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

# minute hour day-of-month month day-of-week  <job>
read -r -d '' JOB_SPEC <<'SPEC' || true
0 7 * * 2 waivers
0 8 * * * injuries
0 10 * * 4 lineup
0 9 * * 0 lineup
0 7 * * 3 byes
0 8 * * 1 recap
15 8 * * 1 trades
SPEC

build_crontab() {
  while read -r m h dom mon dow job; do
    [ -z "${job:-}" ] && continue
    echo "$m $h $dom $mon $dow cd $PROJECT_ROOT && $PYTHON $CLI $job >> $LOG_DIR/$job.log 2>&1 $MARKER"
  done <<< "$JOB_SPEC"
}

# Preserve any crontab lines that are not ours.
EXISTING="$(crontab -l 2>/dev/null | grep -v -- "$MARKER" || true)"

case "${1:-}" in
  --remove)
    printf '%s\n' "$EXISTING" | crontab -
    echo "Removed all $MARKER entries."
    ;;
  --dry-run)
    echo "--- crontab that would be installed ---"
    build_crontab
    ;;
  *)
    { [ -n "$EXISTING" ] && printf '%s\n' "$EXISTING"; build_crontab; } | crontab -
    echo "Installed:"
    build_crontab | sed 's/^/  /'
    echo
    echo "Verify with : crontab -l"
    echo "Remove with : bash jobs/install_schedule.sh --remove"
    echo
    echo "Note: the machine must be awake at these times. If it sleeps, run"
    echo "'$PYTHON $CLI daily' manually instead - every job is idempotent."
    ;;
esac
