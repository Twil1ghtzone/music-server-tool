FROM python:3.12-slim AS base

# ffmpeg/ffprobe  -> Audio-Stream-Hash + technische Metadaten
# libchromaprint-tools -> fpcalc fuer akustische Fingerprints
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libchromaprint-tools \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /data /music /staging /quarantine \
 && chmod 777 /data

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
