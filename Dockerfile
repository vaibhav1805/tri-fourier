FROM python:3.12-slim AS base

# Metadata
LABEL maintainer="AutoTriage Team"
LABEL description="AutoTriage Agent - Kubernetes-first production troubleshooting"

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies required by FalkorDBLite
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --gid 1001 autotriage && \
    useradd --uid 1001 --gid autotriage --shell /bin/bash --create-home autotriage

WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ /app/src/
COPY config/ /app/config/
COPY scripts/ /app/scripts/

# Add src to Python path so 'import triagebot' works
ENV PYTHONPATH="/app/src:${PYTHONPATH}"

# Create data directories for persistence
RUN mkdir -p /app/data/graph /app/data/snapshots /app/logs && \
    chown -R autotriage:autotriage /app

# Switch to non-root user
USER autotriage

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Single worker: FalkorDBLite subprocess is per-process
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
