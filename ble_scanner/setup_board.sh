#!/usr/bin/env bash
# Set up an Arduino UNO Q as an occupancy sensor, from this Mac, over USB.
#
#   ble_scanner/setup_board.sh <board-serial> <device-name> [scan.py options]
#
# Examples:
#   ble_scanner/setup_board.sh 991849120 uno-q-2 --display --threshold -40 --window 6 --interval 1 --send-interval 300
#   ble_scanner/setup_board.sh            # no arguments: list connected boards
#
# Before running it, on the new board (needs its password, so do it yourself):
#   1. first setup in Arduino App Lab (name, password, Wi-Fi)
#   2. sudo apt update && sudo apt full-upgrade -y --allow-downgrades && sudo usermod -aG arduino-router arduino
#   3. eduroam via nmcli (see ble_scanner/README.md), then unplug/replug the board
#
# What this script does:
#   - copies the scanner to ~/ble_scanner and installs its Python packages
#   - uploads the LED matrix sketch (arduino/count_display)
#   - writes ~/ble_scanner/.env from ble_scanner/.env.shared (API key + shared ID secret)
#     plus INGEST_DEVICE=<device-name>
#   - turns on autostart with the given options and (re)starts the scanner
#
# ble_scanner/.env.shared (git-ignored, keep private) must contain:
#   INGEST_API_KEY=...
#   INGEST_ID_SECRET=...   (identical on every board)

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ADB="${ADB:-$HOME/Library/Arduino15/packages/arduino/tools/adb/32.0.0/adb}"
SHARED="$REPO/ble_scanner/.env.shared"
BUILD="$REPO/arduino/count_display/build"
FILES=(scan.py server_client.py mcu_display.py device_kind.py scanner.sh requirements.txt README.md .gitignore)

list_boards() {
  echo "Connected boards (serial: hostname):"
  for s in $("$ADB" devices | awk 'NR>1 && $2=="device"{print $1}'); do
    echo "  $s: $("$ADB" -s "$s" shell hostname | tr -d '\r')"
  done
}

if [[ $# -lt 2 ]]; then
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; echo; list_boards; exit 1
fi
SERIAL="$1"; DEVICE="$2"; shift 2
OPTS=("$@"); [[ ${#OPTS[@]} -eq 0 ]] && OPTS=(--display)
B() { "$ADB" -s "$SERIAL" shell "$@"; }

[[ -f "$SHARED" ]] || { echo "Missing $SHARED (API key + shared ID secret)."; exit 1; }
"$ADB" -s "$SERIAL" get-state >/dev/null 2>&1 || { echo "Board $SERIAL not connected."; list_boards; exit 1; }
echo "== Board $SERIAL ($(B hostname | tr -d '\r')) -> $DEVICE"

echo "== Checking prerequisites"
B 'id' | grep -q arduino-router || echo "   WARNING: user not in arduino-router group (LED won't work). Run step 2 above."
B 'ping -c1 -W3 pypi.org >/dev/null 2>&1' || { echo "   No internet on the board. Connect Wi-Fi first."; exit 1; }

echo "== Copying scanner"
B 'mkdir -p ~/ble_scanner'
( cd "$REPO/ble_scanner" && "$ADB" -s "$SERIAL" push "${FILES[@]}" /home/arduino/ble_scanner/ >/dev/null )
B 'chmod +x ~/ble_scanner/scanner.sh'

echo "== Installing Python packages"
B 'cd ~/ble_scanner && if [ ! -x .venv/bin/pip ]; then rm -rf .venv && python3 -m venv --without-pip .venv && curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && .venv/bin/python /tmp/get-pip.py -q; fi; .venv/bin/pip install -q -r requirements.txt 2>&1 | grep -v notice; .venv/bin/python -c "import bleak, msgpack" && echo "   ok"'

echo "== Writing .env"
{ grep -E '^INGEST_(API_KEY|ID_SECRET|URL)=' "$SHARED"; echo "INGEST_DEVICE=$DEVICE"; } \
  | B 'umask 077; cat > ~/ble_scanner/.env; chmod 600 ~/ble_scanner/.env'

echo "== Uploading LED sketch"
if [[ -f "$BUILD/count_display.ino.elf-zsk.bin" ]]; then
  PORT=$(arduino-cli board list --format json | python3 -c "
import sys, json
for p in json.load(sys.stdin).get('detected_ports', []):
    if p['port'].get('properties', {}).get('serialNumber') == '$SERIAL':
        print(p['port']['address']); break")
  if [[ -n "$PORT" ]]; then
    B '~/ble_scanner/scanner.sh stop' >/dev/null || true
    arduino-cli upload -b arduino:zephyr:unoq -p "$PORT" --input-dir "$BUILD" "$REPO/arduino/count_display" 2>&1 | tail -1
  else echo "   No USB serial port found for $SERIAL, skipping."; fi
else echo "   No prebuilt sketch in $BUILD, skipping (see arduino/count_display)."; fi

echo "== Starting with autostart: ${OPTS[*]}"
B "cd ~/ble_scanner && ./scanner.sh autostart on ${OPTS[*]} && ./scanner.sh restart"
sleep 8
B '~/ble_scanner/scanner.sh status'
echo "== Done: $DEVICE"
