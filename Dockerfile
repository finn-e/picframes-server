FROM python:3.11-slim

# Install system dependencies for Pillow and packaging
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and firmware
COPY app.py .
COPY converters/ ./converters/

# Expose server port
EXPOSE 8080

# Environment variables
ENV PORT=8080
ENV SHARE_DIR=/share
ENV CONFIG_DIR=/config
ENV FIRMWARE_DIR=/app/firmware

# Run Flask app with production gunicorn server
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "app:app"]
