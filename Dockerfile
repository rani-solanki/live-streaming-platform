FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VS_MEDIA_DIR=/media VS_HLS_DIR=/hls

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY frontend frontend

RUN useradd --create-home --uid 1000 vs \
    && mkdir -p /media /hls && chown vs /media /hls
# 'video' group lets the user open /dev/video* when a webcam is passed through (Linux)
RUN usermod -aG video vs
USER vs

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD curl -fs http://localhost:8000/api/health || exit 1

# One worker: the stream registry lives in-process (see README for scaling).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
