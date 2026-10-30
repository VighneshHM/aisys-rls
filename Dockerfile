# ShelfSense demo image: the application with the synthetic demo library, every device and service simulated.
#   docker build -t shelfsense .
#   docker run --rm -p 8000:8000 shelfsense        then open http://localhost:8000
# Data lives in /app/data; mount a volume there to keep it between runs. For a real site use the signed offline
# package instead (docs/deployment.md): this image is for trying the system.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SHELFSENSE_HOST=0.0.0.0 \
    SHELFSENSE_PORT=8000

WORKDIR /app

# Runtime dependencies first, exactly as pinned (with hashes), so this layer is cached between code changes.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 shelfsense \
    && mkdir -p /app/data \
    && chown shelfsense:shelfsense /app/data

USER shelfsense
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# The first start loads the demo library (about 15 seconds); later starts keep what is in /app/data.
CMD ["sh", "-c", "[ -f data/shelfsense.db ] || shelfsense seed-demo --quiet; exec shelfsense run"]
