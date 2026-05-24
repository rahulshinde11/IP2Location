FROM python:3.11-slim

WORKDIR /app

# No system build deps needed: all requirements ship pure-Python wheels, so the
# previous `gcc` install (and its apt layer) is dropped to keep the image lean.

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (includes app.py and entrypoint.py)
COPY . .

ENV PYTHONPATH=/app \
    FLASK_APP=app.py \
    CHOWN_DIRS=/app/logs

# Create the unprivileged user the entrypoint drops to (uid/gid 1000).
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import requests,sys; sys.exit(0 if requests.get('http://localhost:8080/health', timeout=5).status_code==200 else 1)" || exit 1

# Container starts as root so entrypoint.py can chown the mounted logs dir, then
# drops to appuser before exec'ing gunicorn. Concurrency/memory are tuned in
# gunicorn.conf.py (workers, threads, preload_app).
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["gunicorn", "-c", "/app/gunicorn.conf.py", "app:app"]
