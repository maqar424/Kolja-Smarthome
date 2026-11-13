# Lamp remote. Built on the Pi itself (arm64): docker compose up -d --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin \
    DATA_DIR=/data

WORKDIR /app

# Dependencies first: this layer is cached as long as requirements.txt is unchanged,
# so a normal update only copies the code (seconds instead of minutes on the Pi).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY config ./config
COPY docs/apartment-floorplan.svg ./docs/apartment-floorplan.svg

# Run as an unprivileged user (uid 1000 = the default user on Raspberry Pi OS,
# so the ./data folder on the Pi stays writable for both).
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:create_default_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "3"]
