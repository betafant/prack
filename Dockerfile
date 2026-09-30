# prack - multi-arch friendly (works on x86 VPS and Oracle Cloud Ampere/ARM)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRACK_HOST=0.0.0.0 \
    PRACK_PORT=8000 \
    PRACK_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY prack ./prack
RUN pip install ".[postgres]" \
 && useradd --system --uid 1000 --home /data prack \
 && mkdir -p /data && chown prack /data

USER prack
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["prack", "run"]
