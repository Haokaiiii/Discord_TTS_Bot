# Build stage
FROM python:3.10-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev

# Install Python dependencies
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /app/wheels -r requirements.txt

# Final stage
FROM python:3.10-slim

# Create non-root user
RUN useradd -m -u 1000 botuser

WORKDIR /app

# Install runtime dependencies and fonts with better CJK support
RUN apt-get update && apt-get install -y \
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
    libnss3 \
    libasound2 \
    wget \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder
COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .

# Install Python packages
RUN pip install --no-cache /wheels/*

# Copy application
COPY . .

# Create necessary directories and set permissions
RUN mkdir -p /app/tts_cache /app/data_backup /app/mplconfig \
    && chown -R botuser:botuser /app \
    && chmod -R 755 /app

# Create fonts directory with permissions
RUN mkdir -p /usr/share/fonts/truetype/custom && chmod -R 777 /usr/share/fonts/truetype/custom

# Set environment variables
ENV MPLCONFIGDIR=/app/mplconfig
ENV PYTHONUNBUFFERED=1
ENV TZ=Australia/Sydney

# Rebuild font cache
RUN fc-cache -fv

# Switch to non-root user
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health')" || exit 1

# Run application
CMD ["python", "bot.py"]