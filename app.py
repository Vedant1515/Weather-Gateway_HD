"""
app.py — Weather Data Gateway

Flask application that proxies Open-Meteo weather data, persists snapshots
to a Kubernetes PersistentVolume, and serves history + statistics.

Run locally:  python app.py
Production:   gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
"""

import logging
import os
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import storage

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)  # allow fetch() from file:// and any other origin (dashboard + local dev)

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,"
    "precipitation,weather_code"
    "&timezone=auto"
)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"

OPEN_METEO_TIMEOUT = 8  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _success(data, status_code: int = 200, **extra):
    body = {"status": "success", "data": data, "timestamp": _now_iso()}
    body.update(extra)
    return jsonify(body), status_code


def _error(message: str, status_code: int):
    body = {"status": "error", "message": message, "timestamp": _now_iso()}
    return jsonify(body), status_code


def _geocode_city(city: str) -> tuple[float, float, str]:
    """
    Resolve a city name to (latitude, longitude, canonical_name) via Open-Meteo geocoding.

    Raises ValueError if the city is not found, requests.RequestException on network failure.
    """
    url = GEOCODING_URL.format(city=requests.utils.quote(city))
    logger.info("Geocoding city '%s'", city)
    resp = requests.get(url, timeout=OPEN_METEO_TIMEOUT)
    resp.raise_for_status()
    results = resp.json().get("results")
    if not results:
        raise ValueError(f"City '{city}' not found. Check the spelling and try again.")
    r = results[0]
    canonical = r.get("name", city)
    country = r.get("country", "")
    if country:
        canonical = f"{canonical}, {country}"
    return float(r["latitude"]), float(r["longitude"]), canonical


def _fetch_live_weather(lat: float, lon: float) -> dict:
    """
    Call Open-Meteo and return the normalised current-weather dict.

    Raises requests.RequestException on network failure so the caller
    can fall back to cached data.
    """
    url = OPEN_METEO_URL.format(lat=lat, lon=lon)
    logger.info("Fetching live weather from Open-Meteo: %s", url)
    resp = requests.get(url, timeout=OPEN_METEO_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    current = payload.get("current", {})
    return {
        "temperature": current.get("temperature_2m"),
        "humidity": current.get("relative_humidity_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "precipitation": current.get("precipitation"),
        "weather_code": current.get("weather_code"),
        "source": "live",
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Serve the visual test dashboard."""
    return send_file("static/dashboard.html")


@app.route("/", methods=["GET"])
def index():
    """Welcome message listing all available endpoints."""
    logger.info("GET /")
    endpoints = [
        {"method": "GET",    "path": "/",                         "description": "This help page"},
        {"method": "GET",    "path": "/health",                   "description": "Liveness probe"},
        {"method": "GET",    "path": "/ready",                    "description": "Readiness probe (checks PVC)"},
        {"method": "POST",   "path": "/weather/snapshot",         "description": "Fetch + save live weather snapshot (?city=&lat=&lon=)"},
        {"method": "GET",    "path": "/weather/history",          "description": "All saved snapshots"},
        {"method": "GET",    "path": "/weather/latest",           "description": "Most recent snapshot"},
        {"method": "GET",    "path": "/weather/stats",            "description": "Aggregated statistics"},
        {"method": "DELETE", "path": "/weather/<id>",             "description": "Delete a snapshot by id"},
    ]
    return _success(
        data={
            "name": "Weather Data Gateway",
            "version": "1.0.0",
            "description": (
                "Cloud-native REST gateway that proxies Open-Meteo weather data, "
                "persists snapshots to a PersistentVolume, and serves history + stats."
            ),
            "endpoints": endpoints,
        }
    )


@app.route("/health", methods=["GET"])
def health():
    """
    Liveness probe.

    Kubernetes restarts the pod if this returns non-2xx.
    The check is intentionally cheap — we only verify the process is alive.
    """
    logger.debug("GET /health")
    return jsonify({"status": "healthy", "timestamp": _now_iso()}), 200


@app.route("/ready", methods=["GET"])
def ready():
    """
    Readiness probe.

    Kubernetes stops routing traffic to this pod until it returns 200.
    We verify that the PVC mount is actually readable/writable so we only
    accept traffic when we can persist data.
    """
    logger.debug("GET /ready")
    if storage.is_storage_accessible():
        return jsonify({"status": "ready", "timestamp": _now_iso()}), 200
    return jsonify({"status": "not ready", "reason": "PVC storage inaccessible", "timestamp": _now_iso()}), 503


@app.route("/weather/snapshot", methods=["POST"])
def create_snapshot():
    """
    Fetch live weather from Open-Meteo and save a snapshot to PVC.

    Query parameters:
        city (str, required): City name — lat/lon are resolved automatically via geocoding.

    Graceful degradation: if Open-Meteo is unreachable, the last cached
    snapshot for *any* location is returned with source="cached".
    """
    city = request.args.get("city", "").strip()
    if not city:
        return _error("Query parameter 'city' is required (e.g. ?city=Melbourne).", 400)

    logger.info("POST /weather/snapshot city=%s", city)

    # --- Resolve city to coordinates ------------------------------------------
    try:
        lat, lon, city = _geocode_city(city)
        logger.info("Geocoded to lat=%s lon=%s name='%s'", lat, lon, city)
    except ValueError as exc:
        return _error(str(exc), 404)
    except requests.RequestException as exc:
        logger.warning("Geocoding failed (%s) — attempting graceful degradation", exc)
        latest = storage.get_latest()
        if latest:
            return jsonify({
                "status": "degraded",
                "message": "Geocoding service unreachable. Returning last cached snapshot.",
                "data": latest,
                "timestamp": _now_iso(),
            }), 503
        return _error("Geocoding service unreachable and no cached data available.", 503)

    # --- Attempt live fetch ---------------------------------------------------
    try:
        weather = _fetch_live_weather(lat, lon)
    except requests.RequestException as exc:
        logger.warning("Open-Meteo unreachable (%s) — attempting graceful degradation", exc)

        # Graceful degradation: return most recent cached snapshot.
        latest = storage.get_latest()
        if latest:
            logger.info("Returning cached snapshot id=%s as degraded response", latest["id"])
            return jsonify({
                "status": "degraded",
                "message": "Open-Meteo is unreachable. Returning last cached snapshot.",
                "data": latest,
                "timestamp": _now_iso(),
            }), 503

        return _error(
            "Open-Meteo is unreachable and no cached data is available.", 503
        )

    # --- Build and persist snapshot ------------------------------------------
    snapshot = {
        "city": city,
        "latitude": lat,
        "longitude": lon,
        **weather,
        "timestamp": _now_iso(),
    }
    saved = storage.save_snapshot(snapshot)
    return _success(saved, status_code=201)


@app.route("/weather/history", methods=["GET"])
def history():
    """Return all saved snapshots, newest first."""
    logger.info("GET /weather/history")
    snapshots = storage.get_all_snapshots()
    return _success({"count": len(snapshots), "snapshots": snapshots})


@app.route("/weather/latest", methods=["GET"])
def latest():
    """Return the most recent snapshot."""
    logger.info("GET /weather/latest")
    snapshot = storage.get_latest()
    if snapshot is None:
        return _error("No snapshots available.", 404)
    return _success(snapshot)


@app.route("/weather/stats", methods=["GET"])
def stats():
    """Return aggregated statistics across all snapshots."""
    logger.info("GET /weather/stats")
    result = storage.get_stats()
    if result is None:
        return _error("No snapshots available to compute statistics.", 404)
    return _success(result)


@app.route("/weather/<snapshot_id>", methods=["DELETE"])
def delete_snapshot(snapshot_id: str):
    """Delete a specific snapshot by its UUID."""
    logger.info("DELETE /weather/%s", snapshot_id)
    deleted = storage.delete_snapshot(snapshot_id)
    if not deleted:
        return _error(f"Snapshot '{snapshot_id}' not found.", 404)
    return _success({"deleted_id": snapshot_id})


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(exc):
    return _error("Endpoint not found.", 404)


@app.errorhandler(405)
def method_not_allowed(exc):
    return _error("HTTP method not allowed for this endpoint.", 405)


@app.errorhandler(500)
def internal_error(exc):
    logger.exception("Unhandled internal error")
    return _error("Internal server error.", 500)


# ---------------------------------------------------------------------------
# Entry point (local dev only — production uses gunicorn)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    logger.info("Starting Weather Gateway on port %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)
