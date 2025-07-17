FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    cron \
    wget \
    unzip \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

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
    mkdir -p /var/run && \
    touch /var/run/crond.pid && \
    chown -R appuser:appuser /app /downloads /backups

# Copy cron configuration as root
COPY crontab /tmp/crontab
RUN crontab -u appuser /tmp/crontab

# Copy startup script and make it executable
COPY startup.sh /app/startup.sh
RUN chmod +x /app/startup.sh

# Start with our custom startup script
CMD ["/app/startup.sh"] 