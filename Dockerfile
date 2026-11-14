FROM python:3.12-slim

# 国内服务器可在构建时传入镜像源加速：见 deploy.sh --cn
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""

RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s#http://deb.debian.org#${APT_MIRROR}#g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk curl \
 && rm -rf /var/lib/apt/lists/*

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
