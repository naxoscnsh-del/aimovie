FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY videogen ./videogen
COPY web ./web

ENV DATA_DIR=/app/data PORT=8000 PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["python", "-m", "web"]
