FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY app.py .
COPY micropython-firmware ./micropython-firmware

# Expose port
EXPOSE 8000

# Environment variables
ENV PORT=8000
ENV SHARE_DIR=/share

# Run the app
CMD ["python", "app.py"]
