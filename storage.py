"""
storage.py — PersistentVolume file I/O for weather snapshots.

All reads and writes target /data/weather_data.json, which is mounted
from a Kubernetes PersistentVolumeClaim shared across replicas.
A threading.Lock guards in-process concurrency; fcntl advisory locks
guard cross-process (multi-replica) concurrency on the same PVC file.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# In Kubernetes the PVC is mounted at /data.
# Locally fall back to a data/ folder next to this file (works on Windows too).
_here = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get(
    "WEATHER_DATA_DIR",
    "/data" if os.path.exists("/data") else os.path.join(_here, "data"),
)
DATA_FILE = os.path.join(DATA_DIR, "weather_data.json")

# RLock (reentrant) so save_snapshot/delete_snapshot can hold the lock while
# calling _read_data_locked() internally without deadlocking.
_lock = threading.RLock()


def _acquire_file_lock(fh):
    """Acquire an advisory exclusive lock on an open file handle (Unix only)."""
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
    except (ImportError, OSError):
        # fcntl unavailable on Windows (local dev) — rely on threading.Lock alone.
        pass


def _release_file_lock(fh):
    """Release the advisory lock on an open file handle."""
    try:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _ensure_data_file():
    """Create /data/weather_data.json with an empty snapshot list if absent."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as fh:
            json.dump({"snapshots": []}, fh)
        logger.info("Initialised empty data file at %s", DATA_FILE)


def _read_file() -> dict:
    """Read the data file. Caller must hold _lock."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fh:
            _acquire_file_lock(fh)
            data = json.load(fh)
            _release_file_lock(fh)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load data file (%s) — returning empty store", exc)
        return {"snapshots": []}


def load_data() -> dict:
    """
    Read and return the full contents of the data file.

    Returns an empty snapshots structure if the file is missing or corrupt.
    """
    _ensure_data_file()
    with _lock:
        return _read_file()


def _write_data(data: dict):
    """Atomically overwrite the data file with *data* (caller holds _lock)."""
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        _acquire_file_lock(fh)
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
        _release_file_lock(fh)
    # Atomic replace — avoids a partial file being read by another process.
    os.replace(tmp_path, DATA_FILE)


def save_snapshot(snapshot: dict) -> dict:
    """
    Persist a new weather snapshot and return it with an assigned id.

    The snapshot dict must contain at minimum: city, latitude, longitude,
    temperature, humidity, wind_speed, precipitation, weather_code, source.
    """
    _ensure_data_file()
    snapshot["id"] = str(uuid.uuid4())
    snapshot.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    with _lock:
        data = _read_file()
        data["snapshots"].append(snapshot)
        _write_data(data)

    logger.info("Saved snapshot id=%s city=%s", snapshot["id"], snapshot.get("city"))
    return snapshot


def get_all_snapshots() -> list:
    """Return all stored snapshots, newest first."""
    data = load_data()
    snapshots = data.get("snapshots", [])
    return sorted(snapshots, key=lambda s: s.get("timestamp", ""), reverse=True)


def get_latest() -> Optional[dict]:
    """Return the most recently saved snapshot, or None if none exist."""
    snapshots = get_all_snapshots()
    return snapshots[0] if snapshots else None


def get_stats() -> Optional[dict]:
    """
    Compute aggregate statistics across all snapshots.

    Returns avg/min/max for temperature, humidity, and wind_speed,
    plus total snapshot count.  Returns None when no snapshots exist.
    """
    snapshots = get_all_snapshots()
    if not snapshots:
        return None

    temps = [s["temperature"] for s in snapshots if s.get("temperature") is not None]
    humidities = [s["humidity"] for s in snapshots if s.get("humidity") is not None]
    winds = [s["wind_speed"] for s in snapshots if s.get("wind_speed") is not None]

    def _stats(values: list) -> dict:
        if not values:
            return {"avg": None, "min": None, "max": None}
        return {
            "avg": round(sum(values) / len(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
        }

    return {
        "total_snapshots": len(snapshots),
        "temperature": _stats(temps),
        "humidity": _stats(humidities),
        "wind_speed": _stats(winds),
    }


def delete_snapshot(snapshot_id: str) -> bool:
    """
    Remove the snapshot with the given id.

    Returns True if a snapshot was deleted, False if id was not found.
    """
    _ensure_data_file()
    with _lock:
        data = _read_file()
        original_count = len(data.get("snapshots", []))
        data["snapshots"] = [
            s for s in data.get("snapshots", []) if s.get("id") != snapshot_id
        ]
        if len(data["snapshots"]) == original_count:
            return False
        _write_data(data)

    logger.info("Deleted snapshot id=%s", snapshot_id)
    return True


def is_storage_accessible() -> bool:
    """
    Probe that the PVC mount is readable and writable.

    Used by the /ready endpoint so Kubernetes only routes traffic to pods
    that can actually persist data.
    """
    try:
        _ensure_data_file()
        probe_path = os.path.join(DATA_DIR, ".probe")
        with open(probe_path, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe_path)
        return True
    except OSError as exc:
        logger.error("Storage accessibility check failed: %s", exc)
        return False
