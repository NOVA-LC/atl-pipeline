FROM python:3.12-slim

WORKDIR /app

# System deps (none strictly required, but keep slim image working)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects $PORT at runtime. Bind to 0.0.0.0 and use gthread workers for flask-sock WebSocket support.
ENV PYTHONUNBUFFERED=1
EXPOSE 5050
# Shell form so ${PORT} expands at runtime (Railway injects PORT env var).
# Without `sh -c`, exec form would pass `${PORT:-5050}` as a literal string and gunicorn rejects it.
CMD ["/bin/sh", "-c", "exec gunicorn --workers 1 --worker-class gthread --threads 100 --timeout 0 --graceful-timeout 30 --bind 0.0.0.0:${PORT:-5050} server:app"]
