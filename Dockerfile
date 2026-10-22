FROM python:3.13-slim

# FFmpeg is required for audio playback
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. /app/data holds uploads and state, so mount a volume there.
RUN useradd -m bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
