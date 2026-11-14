# 国内服务器：deploy.sh --cn 会把下面三个参数换成国内源（写在 .env 里，更新时自动沿用）
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
      for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "$f" ]; then sed -i -E "s#https?://deb.debian.org#${APT_MIRROR}#g" "$f"; fi; \
      done; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk curl; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+-i $PIP_INDEX_URL} -r requirements.txt

COPY videogen ./videogen
COPY web ./web

ENV DATA_DIR=/app/data PORT=8000 PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["python", "-m", "web"]
