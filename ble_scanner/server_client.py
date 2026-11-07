"""Send scans to the Hackaburger ingest server and apply the config it replies with.

Implements the board side of docs/server-api.md:
  - POST /ingest/data with header X-API-Key
  - failed sends are queued (up to MAX_QUEUE) and resent oldest first
  - the reply's "config" object is validated, clamped and applied; unknown
    fields are ignored, missing fields keep their current value
  - the last config is saved to config.json so it survives a reboot
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
CONFIG_FILE = HERE / "config.json"

DEFAULT_URL = "https://hackaburger.cch-uk.tech/ingest/data"
MAX_QUEUE = 720          # ~1 h at 5 s
MAX_SEND_PER_ROUND = 20  # catch-up limit per interval
AUTH_RETRY_S = 60
TIMEOUT_S = 5

# name: (type, default, min, max)   -- docs/server-api.md, "Settings"
CONFIG_SPEC = {
    "config_version": (int, 0, 0, None),
    "enabled": (bool, True, None, None),
    "threshold_dbm": (int, -75, -100, -30),
    "window_s": (int, 30, 2, 300),
    "interval_s": (float, 5, 1, 600),
    "people_factor": (float, 0.7, 0.1, 2.0),
    "send_devices": (bool, True, None, None),
    "display": (bool, True, None, None),
}


def load_env(path: Path = ENV_FILE) -> dict:
    """Read KEY=VALUE lines from .env (values in the real environment win)."""
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("INGEST_")})
    return env


def default_config() -> dict:
    return {k: spec[1] for k, spec in CONFIG_SPEC.items()}


def merge_config(current: dict, incoming) -> tuple[dict, list[str]]:
    """Apply known, valid fields from `incoming`; clamp numbers to allowed range."""
    new = dict(current)
    changed = []
    if not isinstance(incoming, dict):
        return new, changed
    for key, (typ, _default, lo, hi) in CONFIG_SPEC.items():
        if key not in incoming:
            continue
        val = incoming[key]
        if typ is bool:
            if not isinstance(val, bool):
                continue
        elif isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        else:
            val = typ(val)
            if lo is not None:
                val = max(lo, val)
            if hi is not None:
                val = min(hi, val)
        if new.get(key) != val:
            new[key] = val
            changed.append(key)
    return new, changed


def load_saved_config() -> dict | None:
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return None
    cfg, _ = merge_config(default_config(), saved)
    return cfg


def save_config(cfg: dict):
    try:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(CONFIG_FILE)
    except OSError as e:
        print(f"   [server] could not save config: {e}")


class DeviceIdHasher:
    """Anonymise Bluetooth addresses: first 16 hex chars of HMAC-SHA256(secret, address)."""

    def __init__(self, secret: str):
        self.key = secret.encode()

    def __call__(self, address: str) -> str:
        return hmac.new(self.key, address.upper().encode(), hashlib.sha256).hexdigest()[:16]


class ServerClient:
    def __init__(self, url: str, api_key: str, device: str):
        self.url = url
        self.api_key = api_key
        self.device = device
        self.queue: deque = deque(maxlen=MAX_QUEUE)
        self.auth_blocked_until = 0.0
        self.last_status = "not sent yet"

    def _post(self, body: dict) -> tuple[int, dict | None]:
        data = json.dumps(body, separators=(",", ":")).encode()
        req = urllib.request.Request(self.url, data=data, method="POST", headers={
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "hackaburger-uno-q/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            return e.code, None
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, None

    def send(self, message: dict) -> dict | None:
        """Queue `message`, send queued messages oldest first.

        Blocking (run it in a thread). Returns the newest `config` object the
        server replied with, or None.
        """
        self.queue.append(message)
        if time.time() < self.auth_blocked_until:
            self.last_status = f"auth failed, retrying later ({len(self.queue)} queued)"
            return None

        newest_config = None
        sent = 0
        while self.queue and sent < MAX_SEND_PER_ROUND:
            body = self.queue[0]
            try:
                status, reply = self._post(body)
            except (OSError, urllib.error.URLError) as e:
                self.last_status = f"offline ({e.__class__.__name__}), {len(self.queue)} queued"
                break

            if status in (200, 201):
                self.queue.popleft()
                sent += 1
                if isinstance(reply, dict) and isinstance(reply.get("config"), dict):
                    newest_config = reply["config"]
                self.last_status = f"ok ({status})" + (f", {len(self.queue)} queued" if self.queue else "")
            elif status == 413 and "devices" in body.get("data", {}):
                trimmed = {**body, "data": {k: v for k, v in body["data"].items() if k != "devices"}}
                self.queue[0] = trimmed
                self.last_status = "413 too large, resending without devices"
            elif status in (400, 413, 422):
                self.queue.popleft()
                self.last_status = f"rejected ({status}), message dropped"
            elif status in (401, 403):
                self.auth_blocked_until = time.time() + AUTH_RETRY_S
                self.last_status = f"auth failed ({status}), check INGEST_API_KEY"
                break
            else:
                self.last_status = f"server error ({status}), {len(self.queue)} queued"
                break
        return newest_config
