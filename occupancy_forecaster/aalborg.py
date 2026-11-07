"""Aalborg office dataset: download, verify and convert to the forecaster's CSV format.

Source: "A Danish high-resolution dataset for six office rooms with occupancy, indoor
environment, heating, ventilation, lighting and room control monitoring", Aalborg
University, Zenodo, doi:10.5281/zenodo.10673763, licence CC BY 4.0.

Six offices (Room A-F), 5-minute resolution, 27 Feb - 31 Dec 2023. People counts come
from cameras and are missing for whole days/weeks; missing slots stay missing (they are
not zero). Timestamps are written as naive Europe/Copenhagen wall-clock time, matching
read_series() and the weekday/time-of-day features.

    python aalborg.py                      # -> data/aalborg_occupancy.csv
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

import pandas as pd

RECORD_ID = '10673763'
FILE_NAME = 'dataset__2023_02_27__2023_12_31.csv'
DOI = '10.5281/zenodo.10673763'
TIMEZONE = 'Europe/Copenhagen'
ROOMS = 'ABCDEF'


def room_id(letter: str) -> str:
    return f'aalborg-{letter.lower()}'


def download(dest_dir: str | Path) -> Path:
    """Fetch the full-year CSV from Zenodo unless a verified copy is already there."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / FILE_NAME
    with urllib.request.urlopen(f'https://zenodo.org/api/records/{RECORD_ID}', timeout=30) as r:
        record = json.load(r)
    entry = next(f for f in record['files'] if f['key'] == FILE_NAME)
    algo, expected = entry['checksum'].split(':', 1)
    if path.exists() and _digest(path, algo) == expected:
        return path
    tmp = path.with_suffix('.part')
    urllib.request.urlretrieve(entry['links']['self'], tmp)
    if _digest(tmp, algo) != expected:
        tmp.unlink()
        raise ValueError(f'Checksum mismatch for {FILE_NAME}')
    tmp.replace(path)
    return path


def _digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_wide(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, sep=';', encoding='utf-8-sig', index_col=0)
    df = df.dropna(subset=['timestamp'])  # the file has a few completely empty rows
    df.index = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert(TIMEZONE)
    return df.drop(columns='timestamp')


def to_long(wide: pd.DataFrame) -> pd.DataFrame:
    """One row per room and 5-minute slot that has a people count."""
    parts = []
    local = wide.index.tz_localize(None)  # wall-clock time; DST fall-back duplicates are median-merged later
    for letter in ROOMS:
        part = pd.DataFrame({
            'timestamp': local,
            'room_id': room_id(letter),
            'count': wide[f'Room{letter}:People__amount'].to_numpy(),
            'co2_ppm': wide[f'Room{letter}:Sensor__CO2'].to_numpy(),
            'window_open': wide[f'Room{letter}:Window__opened_closed'].to_numpy(),
        })
        parts.append(part.dropna(subset=['count']))
    return pd.concat(parts, ignore_index=True)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--raw-dir', default='data/raw')
    p.add_argument('--out', default='data/aalborg_occupancy.csv')
    args = p.parse_args()
    raw = download(args.raw_dir)
    long = to_long(load_wide(raw))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    long.to_csv(args.out, index=False)
    summary = long.groupby('room_id')['count'].agg(['count', 'max', 'mean'])
    print(f'Wrote {len(long):,} rows to {args.out} (source: doi:{DOI}, CC BY 4.0)')
    print(summary.round(2).to_string())


if __name__ == '__main__':
    main()
