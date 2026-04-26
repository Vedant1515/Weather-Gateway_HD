FROM python:3.11-slim

# Keeps Python from buffering stdout/stderr so logs appear immediately in kubectl logs.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies before copying source so this layer is cached across rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY app.py storage.py ./
COPY static/ static/

# /data is the PersistentVolumeClaim mount point.
# Creating it here ensures the directory exists when running without a PVC (local Docker).
RUN mkdir -p /data

EXPOSE 5000

# Liveness probe target — Docker will mark the container unhealthy if /health fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" \
    || exit 1

# Two workers: enough concurrency for assignment demo without overwhelming a small GKE node.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "--access-logfile", "-", "app:app"]
