#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="${BOT_SESSION_NAME:-upbit_bot}"
PYTHON_BIN="${BOT_PYTHON_BIN:-python3}"
LOG_DIR="${BOT_LOG_DIR:-$SCRIPT_DIR/logs}"
LOG_FILE="${BOT_LOG_FILE:-$LOG_DIR/runtime.out}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command>

Commands:
  start     Start bot in detached screen session ($SESSION_NAME)
  stop      Stop bot session and related process
  restart   Restart bot
  status    Show bot session/process status
  logs      Follow runtime log ($LOG_FILE)
EOF
}

screen_running() {
  screen -ls 2>/dev/null | grep -q "[.]${SESSION_NAME}[[:space:]]"
}

process_running() {
  pgrep -f "$SCRIPT_DIR.*main.py" >/dev/null 2>&1
}

ensure_prereqs() {
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON_BIN is not installed."
    exit 1
  fi

  if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "ERROR: config.json is missing in $SCRIPT_DIR"
    echo "       Copy config.example.json to config.json and set keys."
    exit 1
  fi

  mkdir -p "$LOG_DIR"
  touch "$LOG_FILE"
}

start_bot() {
  ensure_prereqs

  if screen_running || process_running; then
    echo "Bot already running."
    status_bot
    return 0
  fi

  screen -dmS "$SESSION_NAME" bash -lc "cd \"$SCRIPT_DIR\" && \"$PYTHON_BIN\" main.py >> \"$LOG_FILE\" 2>&1"
  sleep 1
  echo "Bot started."
  status_bot
}

stop_bot() {
  local stopped=0

  if screen_running; then
    screen -S "$SESSION_NAME" -X quit || true
    stopped=1
  fi

  if process_running; then
    pkill -f "$SCRIPT_DIR.*main.py" || true
    stopped=1
  fi

  if [ "$stopped" -eq 1 ]; then
    sleep 1
    echo "Bot stopped."
  else
    echo "Bot is not running."
  fi

  status_bot
}

status_bot() {
  local has_screen=0
  local has_proc=0

  if screen_running; then
    has_screen=1
  fi
  if process_running; then
    has_proc=1
  fi

  if [ "$has_screen" -eq 1 ] || [ "$has_proc" -eq 1 ]; then
    echo "Status: RUNNING"
  else
    echo "Status: STOPPED"
  fi

  echo "Session: $SESSION_NAME"
  screen -ls 2>/dev/null | grep "${SESSION_NAME}" || true
  echo "Process:"
  ps aux | grep "[m]ain.py" | grep "$SCRIPT_DIR" || true
  echo "Log file: $LOG_FILE"
}

logs_bot() {
  mkdir -p "$LOG_DIR"
  touch "$LOG_FILE"
  tail -f "$LOG_FILE"
}

restart_bot() {
  stop_bot
  start_bot
}

main() {
  if [ "${1:-}" = "" ]; then
    usage
    exit 1
  fi

  case "$1" in
    start)
      start_bot
      ;;
    stop)
      stop_bot
      ;;
    restart)
      restart_bot
      ;;
    status)
      status_bot
      ;;
    logs)
      logs_bot
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
