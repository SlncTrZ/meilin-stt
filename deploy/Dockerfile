# meilin-stt — image CPU-only. Model KHÔNG nằm trong image (download lúc start hoặc mount volume).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STT_HOST=0.0.0.0 \
    STT_PORT=8766 \
    STT_MODEL=68m \
    STT_NUM_THREADS=4

# ffmpeg: cần cho /v1/transcribe với mp3/m4a/mp4.
# libsndfile1: soundfile đọc wav/flac/ogg.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY server ./server
COPY lexicon ./lexicon
COPY nightly ./nightly
COPY scripts ./scripts
COPY models ./models

RUN pip install --no-cache-dir . \
    && python scripts/download-models.py --model 68m

# Chạy bằng user thường; /data là nơi duy nhất ghi được (mount volume).
RUN useradd -m -u 10001 meilin \
    && mkdir -p /data /home/meilin/.cache \
    && chown -R meilin:meilin /app /data /home/meilin
USER meilin

# Fail-closed guard trong app yêu cầu STT_ALLOW_PUBLIC=1 khi bind ngoài loopback.
ENV STT_ALLOW_PUBLIC=1 \
    STT_ADMIN_LOOPBACK_ONLY=1

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8766/health || exit 1

CMD ["python", "-m", "server.app"]
