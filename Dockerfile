FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY clipmatch/ clipmatch/
RUN python -m pip install --no-cache-dir ".[live,transcription]"
CMD ["clipmatch-live", "watch"]
