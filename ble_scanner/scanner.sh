#!/usr/bin/env bash
# Control the BLE occupancy scanner on the UNO Q.
#
#   ./scanner.sh start [scan.py options]   start in the background (no options: saved ones, see autostart)
#   ./scanner.sh stop                      stop it
#   ./scanner.sh restart [options]         stop, then start
#   ./scanner.sh status                    is it running? + last report
#   ./scanner.sh log                       follow the live output (Ctrl+C to leave; scanner keeps running)
#   ./scanner.sh run [options]             run in the foreground (Ctrl+C stops it)
#   ./scanner.sh autostart on [options]    start automatically at boot with these options
#                                          (also used by a plain './scanner.sh start')
#   ./scanner.sh autostart off             no autostart (saved options are kept)
#
# Options are passed to scan.py, e.g.: ./scanner.sh start --display --threshold -65
# If .env exists, --send is added automatically; add --local to run without sending.

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
PIDFILE="$DIR/scanner.pid"
LOGFILE="$DIR/scanner.log"
ARGSFILE="$DIR/start.args"   # options saved by 'autostart on', one per line
DEFAULT_ARGS=(--display)
if [[ -f "$ARGSFILE" ]]; then
  DEFAULT_ARGS=(); while IFS= read -r a; do [[ -n "$a" ]] && DEFAULT_ARGS+=("$a"); done <"$ARGSFILE"
fi

is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if is_running; then echo "Already running (pid $(cat "$PIDFILE"))."; return; fi
  local args=("$@"); [[ ${#args[@]} -eq 0 ]] && args=("${DEFAULT_ARGS[@]}")
  # Send to the server whenever .env exists, unless --local is given.
  if [[ " ${args[*]} " == *" --local "* ]]; then
    local kept=(); for a in "${args[@]}"; do [[ "$a" != "--local" ]] && kept+=("$a"); done; args=("${kept[@]}")
  elif [[ -f "$DIR/.env" && " ${args[*]} " != *" --send "* ]]; then
    args+=(--send)
  fi
  cd "$DIR" || exit 1
  nohup "$PY" -u scan.py "${args[@]}" >"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 1
  if is_running; then echo "Started (pid $!): scan.py ${args[*]}"; echo "Log: $LOGFILE"
  else echo "Failed to start:"; tail -n 20 "$LOGFILE"; rm -f "$PIDFILE"; fi
}

stop() {
  if ! is_running; then echo "Not running."; rm -f "$PIDFILE"; return; fi
  local pid; pid=$(cat "$PIDFILE")
  kill -INT "$pid"
  for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  rm -f "$PIDFILE"; echo "Stopped."
}

status() {
  if is_running; then
    echo "Running (pid $(cat "$PIDFILE")): $(tr '\0' ' ' </proc/"$(cat "$PIDFILE")"/cmdline)"
    grep "in range" "$LOGFILE" | tail -n 1
    grep "\[server\]" "$LOGFILE" | tail -n 1
  else echo "Not running."; fi
}

autostart() {
  local line="@reboot sleep 20 && $DIR/scanner.sh start"
  local mode="${1:-}"; shift || true
  case "$mode" in
    on)  if [[ $# -gt 0 ]]; then printf '%s\n' "$@" >"$ARGSFILE"; fi
         (crontab -l 2>/dev/null | grep -v "scanner.sh start"; echo "$line") | crontab -
         echo "Autostart on (~20 s after boot) with: $(tr '\n' ' ' <"$ARGSFILE" 2>/dev/null || echo "${DEFAULT_ARGS[*]}")" ;;
    off) crontab -l 2>/dev/null | grep -v "scanner.sh start" | crontab -
         echo "Autostart off." ;;
    *)   echo "usage: $0 autostart on [options] | off"; exit 1 ;;
  esac
}

cmd="${1:-}"; shift || true
case "$cmd" in
  start)     start "$@" ;;
  stop)      stop ;;
  restart)   stop; start "$@" ;;
  status)    status ;;
  log)       tail -n 30 -f "$LOGFILE" ;;
  run)       cd "$DIR" && exec "$PY" -u scan.py "$@" ;;
  autostart) autostart "$@" ;;
  *)         sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
