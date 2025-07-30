FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    wget \
    unzip \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs /downloads /backups

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Create non-root user and give permissions
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app /downloads /backups && \
    chmod +x /app/updater.py && \
    mkdir -p /app/logs && \
    chown appuser:appuser /app/logs

# Copy startup script and make it executable (as root)
COPY startup.sh /app/startup.sh
RUN chmod +x /app/startup.sh

# Switch to non-root user
USER appuser

# Start with our custom startup script
CMD ["/app/startup.sh"] 