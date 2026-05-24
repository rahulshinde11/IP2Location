FROM python:3.11-slim

WORKDIR /app

# Build deps only; extraction and scheduling are handled in Python now.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (includes updater.py and entrypoint.py)
COPY . .

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    CHOWN_DIRS=/app/db:/downloads:/backups:/app/logs

# Create the unprivileged user the entrypoint drops to (uid/gid 1000).
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/logs /downloads /backups /app/db \
    && chown -R appuser:appuser /app /downloads /backups

# NOTE: we intentionally do NOT set `USER appuser`. The container starts as root
# so entrypoint.py can chown the bind-mounted volumes, then it drops to appuser
# before exec'ing the command below.
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["python3", "updater.py", "--loop"]
