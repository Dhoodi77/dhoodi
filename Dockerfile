FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/ ./scripts/

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Create non-root user for security
RUN useradd -m -u 1000 tradeos && \
    mkdir -p /app/data && \
    chown -R tradeos:tradeos /app

USER tradeos

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8420/health')" || exit 1

EXPOSE 8420

CMD ["python3", "-m", "tradeos.main"]
