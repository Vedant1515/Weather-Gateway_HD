# Weather Data Gateway

A cloud-native REST API gateway that fetches real-time weather data from the
[Open-Meteo](https://open-meteo.com/) API (free, no key required), persists
snapshots to a Kubernetes PersistentVolume, and serves history and aggregated
statistics to clients.

Built for **SIT323/SIT737 Cloud Native Application Development — Task 7.2HD**.

---

## Architecture

```
Client
  │
  ▼
LoadBalancer Service (port 80)
  │
  ├─▶ Pod 1 (weather-gateway) ─┐
  │                             ├─▶ /data/weather_data.json  (PVC)
  └─▶ Pod 2 (weather-gateway) ─┘
            │
            ▼
    Open-Meteo API (external)
```

- **2 replicas** share a single `ReadWriteOnce` PVC via file locking (`fcntl` +
  `threading.Lock`) to prevent write corruption.
- **Rolling update** (`maxUnavailable: 0`) ensures zero downtime during deploys.
- **Graceful degradation**: if Open-Meteo is unreachable, the most recent cached
  snapshot is returned with `source: "cached"` and HTTP 503.
- **`/ready`** blocks Kubernetes traffic until the PVC mount is confirmed
  readable/writable.

---

## Project Structure

```
weather-gateway/
├── app.py              # Flask application — all endpoints
├── storage.py          # PVC file I/O with locking
├── requirements.txt
├── Dockerfile
├── cloudbuild.yaml     # Google Cloud Build CI/CD pipeline
├── .dockerignore
├── .gitignore
└── k8s/
    ├── pvc.yaml        # PersistentVolumeClaim (1 Gi)
    ├── deployment.yaml # 2 replicas, probes, resource limits
    └── service.yaml    # LoadBalancer on port 80
```

---

## Local Development

### Prerequisites
- Python 3.11+
- `pip`

### Setup

```bash
cd weather-gateway

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# The app writes to /data — create it locally
mkdir -p /data                    # Windows: md C:\data  (or set DATA_DIR env var)
```

### Run the development server

```bash
python app.py
# Server starts at http://localhost:5000
```

---

## Docker — Build and Run Locally

### Build

```bash
docker build -t weather-gateway:latest .
```

### Run

```bash
# Mount a local directory as the PVC substitute
docker run -d \
  --name weather-gateway \
  -p 5000:5000 \
  -v "$(pwd)/data:/data" \
  weather-gateway:latest
```

### Verify

```bash
curl http://localhost:5000/health
# {"status":"healthy", ...}

curl http://localhost:5000/ready
# {"status":"ready", ...}
```

### Stop and remove

```bash
docker rm -f weather-gateway
```

---

## Docker Hub — Push

```bash
# Replace vedant1515 with your actual Docker Hub username
docker tag weather-gateway:latest vedant1515/weather-gateway:latest
docker push vedant1515/weather-gateway:latest
```

Update `k8s/deployment.yaml` line `image:` to match your Docker Hub image path.

---

## GKE Deployment

### Prerequisites
- `gcloud` CLI authenticated
- `kubectl` configured for your cluster
- GKE cluster running (e.g. `weather-gateway-cluster` in `australia-southeast1-a`)

### One-time cluster setup

```bash
gcloud container clusters get-credentials weather-gateway-cluster \
  --zone australia-southeast1-a \
  --project YOUR_GCP_PROJECT_ID
```

### Deploy

```bash
# Apply all manifests (PVC, Deployment, Service)
kubectl apply -f k8s/

# Watch rollout
kubectl rollout status deployment/weather-gateway

# Get the external IP (may take 1-2 minutes for LoadBalancer provisioning)
kubectl get service weather-gateway-service
```

### Update image after a new push

```bash
kubectl set image deployment/weather-gateway \
  weather-gateway=vedant1515/weather-gateway:latest

kubectl rollout status deployment/weather-gateway
```

### Cloud Build (CI/CD)

```bash
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions _CLUSTER_NAME=weather-gateway-cluster,_CLUSTER_ZONE=australia-southeast1-a \
  .
```

---

## API Endpoints

Replace `<BASE>` with `http://localhost:5000` (local) or the LoadBalancer
external IP on port 80 (GKE).

### `GET /`
Welcome message + endpoint list.

```bash
curl <BASE>/
```

---

### `GET /health`
Liveness probe — always returns 200 if the process is alive.

```bash
curl <BASE>/health
# {"status": "healthy", "timestamp": "..."}
```

---

### `GET /ready`
Readiness probe — returns 200 only when PVC storage is accessible.

```bash
curl <BASE>/ready
# 200: {"status": "ready", ...}
# 503: {"status": "not ready", "reason": "PVC storage inaccessible", ...}
```

---

### `POST /weather/snapshot`
Fetch live weather from Open-Meteo and save a snapshot.

**Query parameters:**

| Parameter | Required | Description |
|-----------|----------|-------------|
| `city`    | No       | Human-readable label (default: `Unknown`) |
| `lat`     | Yes      | Latitude (float) |
| `lon`     | Yes      | Longitude (float) |

```bash
# Melbourne
curl -X POST "<BASE>/weather/snapshot?city=Melbourne&lat=-37.81&lon=144.96"

# Sydney
curl -X POST "<BASE>/weather/snapshot?city=Sydney&lat=-33.87&lon=151.21"

# London
curl -X POST "<BASE>/weather/snapshot?city=London&lat=51.51&lon=-0.13"
```

**Response (201):**
```json
{
  "status": "success",
  "data": {
    "id": "a1b2c3d4-...",
    "city": "Melbourne",
    "latitude": -37.81,
    "longitude": 144.96,
    "temperature": 18.4,
    "humidity": 62,
    "wind_speed": 14.2,
    "precipitation": 0.0,
    "weather_code": 1,
    "source": "live",
    "timestamp": "2026-04-25T10:00:00+00:00"
  },
  "timestamp": "2026-04-25T10:00:00+00:00"
}
```

**Degraded response (503)** when Open-Meteo is unreachable:
```json
{
  "status": "degraded",
  "message": "Open-Meteo is unreachable. Returning last cached snapshot.",
  "data": { "source": "live", ... },
  "timestamp": "..."
}
```

---

### `GET /weather/history`
All saved snapshots, newest first.

```bash
curl <BASE>/weather/history
```

---

### `GET /weather/latest`
Most recent snapshot.

```bash
curl <BASE>/weather/latest
```

---

### `GET /weather/stats`
Aggregated statistics across all snapshots.

```bash
curl <BASE>/weather/stats
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "total_snapshots": 5,
    "temperature": {"avg": 17.8, "min": 12.1, "max": 24.3},
    "humidity":    {"avg": 58.4, "min": 42.0, "max": 75.0},
    "wind_speed":  {"avg": 11.2, "min": 5.0,  "max": 22.7}
  },
  "timestamp": "..."
}
```

---

### `DELETE /weather/<id>`
Delete a specific snapshot by UUID.

```bash
curl -X DELETE <BASE>/weather/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

---

## Testing Graceful Degradation

Graceful degradation activates when Open-Meteo is unreachable. You can simulate
this locally by blocking the domain:

```bash
# 1. Save at least one snapshot first
curl -X POST "http://localhost:5000/weather/snapshot?city=Melbourne&lat=-37.81&lon=144.96"

# 2. Block outbound access to Open-Meteo (macOS/Linux)
sudo iptables -A OUTPUT -d api.open-meteo.com -j DROP

# 3. Trigger another snapshot — should return HTTP 503 with cached data
curl -X POST "http://localhost:5000/weather/snapshot?city=Sydney&lat=-33.87&lon=151.21"

# 4. Remove the block
sudo iptables -D OUTPUT -d api.open-meteo.com -j DROP
```

On **Windows/Docker**, run the container with no internet (`--network none`) and
observe the degraded 503 response with the last cached snapshot in the body.

---

## Assumptions and Design Decisions

| Decision | Rationale |
|----------|-----------|
| `fcntl` file locking | Two replicas share one `ReadWriteOnce` PVC file; byte-range locking prevents torn writes. Falls back silently on Windows (local dev uses `threading.Lock` only). |
| Atomic write via `.tmp` + `os.replace` | Prevents a replica reading a partially written file during a concurrent save. |
| `maxUnavailable: 0` | Zero-downtime rolling updates; required baseline for Part 2 (10.2HD) health-probe + scaling tasks. |
| `replicas: 2` from day 1 | Demonstrates HA design; Part 2 will scale this up and add HPA. |
| Open-Meteo (no API key) | Assignment constraint — avoids key management complexity for Part 1. |
| `source` field in snapshot | Allows clients and the demo video (Part 3) to visually distinguish live vs cached responses. |
| `/ready` checks PVC write | Prevents a pod with a broken volume from receiving traffic, which would silently lose data. |

---

## Useful kubectl Commands

```bash
# Live pod logs
kubectl logs -l app=weather-gateway -f

# Describe deployment (see probe status, rollout events)
kubectl describe deployment weather-gateway

# Scale manually (Part 2 will add HPA)
kubectl scale deployment weather-gateway --replicas=3

# Port-forward for local testing without a LoadBalancer
kubectl port-forward deployment/weather-gateway 5000:5000
```

<!-- CI/CD trigger test - Week 7 submission -->