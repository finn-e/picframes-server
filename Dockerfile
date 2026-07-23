# ---- Stage 1: compile the Rust dither wheel ----
FROM python:3.11 AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install Rust (minimal profile — compiler + cargo only)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:$PATH"

RUN pip install --no-cache-dir maturin

WORKDIR /build/dither_rs
COPY dither_rs/ .
RUN maturin build --release --interpreter python3.11 --out /wheels

# ---- Stage 2: runtime image ----
FROM python:3.11-slim

# Runtime system deps for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the pre-built Rust wheel from the builder stage
COPY --from=builder /wheels/ /wheels/
RUN pip install --no-cache-dir /wheels/*.whl

# Copy source code and firmware
# Same image is used for both the Flask server and the Celery worker (different CMD)
COPY app.py db.py tasks.py ./
COPY image/ ./image/
COPY routes/ ./routes/
COPY templates/ ./templates/
COPY converters/ ./converters/

EXPOSE 8080

# CI-computed semver injected at build time; app.py falls back to its
# hardcoded SERVER_VERSION when this is empty (e.g. local builds).
ARG SERVER_VERSION_OVERRIDE=""
ENV SERVER_VERSION_OVERRIDE=${SERVER_VERSION_OVERRIDE}

ENV PORT=8080
ENV SHARE_DIR=/share
ENV CONFIG_DIR=/config
ENV FIRMWARE_DIR=/app/firmware

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "app:app"]
