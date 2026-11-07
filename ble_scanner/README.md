# BLE occupancy scanner

Counts nearby Bluetooth Low Energy devices and estimates how many people are in the room.
For now it only prints results locally and sends nothing over the network.

## Run on the Arduino UNO Q (Linux side)

```bash
scp -r ble_scanner arduino@<board-name>.local:~/
ssh arduino@<board-name>.local
cd ble_scanner
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scan.py -v
```

If you get a permission error, add your user to the `bluetooth` group:
`sudo usermod -aG bluetooth $USER`, then log out and back in.

## Control it (start, stop, status)

On the board, `scanner.sh` runs the scanner in the background, so it keeps going after you close the shell:

```bash
./scanner.sh start                          # default: --display -v
./scanner.sh start --display --threshold -65
./scanner.sh status                         # running? + last count
./scanner.sh log                            # live output (Ctrl+C leaves, scanner keeps running)
./scanner.sh stop
./scanner.sh run --threshold -70 -v         # foreground, Ctrl+C stops
./scanner.sh autostart on                   # start automatically after every boot (off to disable)
```

From the Mac, over USB (App Lab installs `adb`):

```bash
ADB=~/Library/Arduino15/packages/arduino/tools/adb/32.0.0/adb
$ADB shell '~/ble_scanner/scanner.sh status'
$ADB shell                                  # interactive shell on the board
```

## Run on a Mac (for testing)

```bash
python3 -m venv .venv && .venv/bin/pip install -r ble_scanner/requirements.txt
.venv/bin/python ble_scanner/scan.py -v
```

macOS asks for Bluetooth permission for your terminal app the first time.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--threshold` | -75 | Min smoothed RSSI (dBm) to count a device. Controls the range (≈ -60: 1–3 m, -70: 3–6 m, -80: 8–15 m). |
| `--window` | 60 | Seconds a device stays counted after its last advert. |
| `--interval` | 10 | Seconds between reports. |
| `--people-factor` | 0.7 | People per counted device. Calibrate against a real headcount. |
| `-v` | off | List each counted device with its RSSI. |

## Calibrating the range

Stand at the edge of the room with your phone and run `scan.py -v --threshold -100`.
Note your phone's RSSI (find it by moving closer or further away), then set `--threshold` a few dB above that value.

## Show the count on the LED matrix

The sketch in [`arduino/count_display`](../arduino/count_display/count_display.ino) runs on the UNO Q's microcontroller. It provides a `show_count` RPC method and draws the number on the built-in 8×13 LED matrix.
Upload it once, then run the scanner with `--display`:

```bash
.venv/bin/python scan.py --display --threshold -70
```

The matrix shows the number of devices within `--threshold`. The bottom-right pixel blinks on each update. A single centre dot means the sketch is waiting for its first count.
