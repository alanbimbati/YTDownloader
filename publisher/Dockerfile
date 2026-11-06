FROM python:3.11-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/publisher/requirements.txt
RUN pip install --no-cache-dir -r /app/publisher/requirements.txt

COPY . /app/publisher

# The container runs as the Syncthing user: the volume must be writable for it (whisper model cache, work files).
RUN mkdir -p /data && chmod 777 /data
ENV PYTHONUNBUFFERED=1 HF_HOME=/data/hf

CMD ["python", "-m", "publisher.main"]
