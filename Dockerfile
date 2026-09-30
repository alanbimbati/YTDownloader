FROM python:3.11-slim

WORKDIR /app

# Install system deps: ffmpeg, ca-certificates, curl + unzip (needed for Deno)
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
  && rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp as JavaScript runtime for YouTube signature challenges)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]

