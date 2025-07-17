FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user first
RUN useradd -m -u 1000 appuser

# Create logs directory and set proper ownership
RUN mkdir -p logs && chown -R appuser:appuser /app

# Set environment variables
ENV PYTHONPATH=/app
ENV FLASK_APP=app.py

# Expose port
EXPOSE 8080

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health')" || exit 1

# Run the application
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app"] 