# Build stage
FROM python:3.10-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
# Consider adding --prefer-binary for faster builds if wheels are available
# networkx might need gcc/dev headers, ensure they are installed in builder stage
RUN pip wheel --no-cache-dir --wheel-dir /app/wheels -r requirements.txt

# Final stage
FROM python:3.10-slim

# Create non-root user
RUN useradd --system --create-home --uid 1000 botuser

WORKDIR /app

# Install runtime dependencies and fonts with better CJK support
# Combine RUN commands and clean up apt cache
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libnss3 \
    libasound2 \
    fontconfig \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fonts-noto-color-emoji \
    fonts-wqy-microhei \
    fonts-wqy-zenhei \
    fonts-arphic-ukai \
    fonts-arphic-uming \
    ca-certificates \
    && fc-cache -fv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder
COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir /wheels/*

# Copy application code (after installing dependencies)
# Copy specific directories/files instead of '.' for better cache utilization
COPY main.py ./
COPY utils/ ./utils/
COPY cogs/ ./cogs/
# COPY .env ./
# >>> WARNING <<<: Copying the .env file directly into the image is a security risk!
# Secrets like your bot token and database URI should ideally be injected at runtime
# using Docker secrets, Kubernetes secrets, or environment variables passed via 
# `docker run -e VAR=value ...` or a docker-compose.yml file.
# Only uncomment the `COPY .env ./` line above if you understand the risks 
# and are in a controlled development environment.

# Create necessary directories and set permissions
# Ensure these match the paths used in config.py (if different from WORKDIR)
RUN mkdir -p /app/tts_cache /app/data_backup /app/mplconfig \
    && chown -R botuser:botuser /app \
    && chmod -R 700 /app/tts_cache /app/data_backup /app/mplconfig \
    && chmod 755 /app/main.py \
    && chmod -R 755 /app/utils /app/cogs

# Set environment variables (MPLCONFIGDIR helps matplotlib find a writable dir)
ENV MPLCONFIGDIR=/app/mplconfig
ENV PYTHONUNBUFFERED=1
ENV TZ=Australia/Sydney
# Consider setting PYTHONIOENCODING=UTF-8

# Rebuild font cache (already done during install)
# RUN fc-cache -fv

# Switch to non-root user
USER botuser

# Health check (adjust port if changed in config.py)
# Use curl or wget if requests isn't guaranteed to be available early
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/health || exit 1
# Or: CMD curl --fail http://localhost:8080/health || exit 1

# Run application using the new entrypoint
CMD ["python", "main.py"]