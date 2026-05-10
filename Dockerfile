FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync ca-certificates tzdata busybox unzip curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/deckmover

# install python dependencies (only what we need for SQLite)
RUN pip install --no-cache-dir requests croniter flask

# our runtime helpers
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY run_once.sh   /usr/local/bin/run_once.sh
RUN chmod +x /usr/local/bin/*.sh

# Copy our SQLite selector scripts (all users via database)
COPY selector_sqlite.py /opt/deckmover/selector_sqlite.py
COPY selector_watched_back_sqlite.py /opt/deckmover/selector_watched_back_sqlite.py
COPY webui.py /opt/deckmover/webui.py
COPY icon.png /opt/deckmover/icon.png

# sane defaults for Unraid
ENV TZ=Australia/Sydney
ENV PYTHONUNBUFFERED=1
ENV DECKMOVER_ARRAY_ROOT=/mnt/user0
ENV DECKMOVER_CACHE_ROOT=/mnt/cache
ENV DECKMOVER_CONFIG=/config/deckmover_settings.json
ENV DECKMOVER_LOG=/logs/deckmover.log
ENV DECKMOVER_TIME=03:15
ENV DECKMOVER_CRON=
ENV DECKMOVER_RUN_IMMEDIATELY=false
ENV DECKMOVER_LOG_LEVEL=info
ENV DECKMOVER_PLEXDB_PATH=/plexdb
ENV DECKMOVER_ONDECK_COUNT=10
ENV DECKMOVER_MAX_ITEMS=100
ENV DECKMOVER_MIN_FREE_GB=20
ENV DECKMOVER_RESERVE_GB=10
ENV DECKMOVER_WARM_MOVE=true
ENV DECKMOVER_WARM_SIDECARS=true
ENV DECKMOVER_MOVE_WATCHED_BACK=false
ENV DECKMOVER_MOVE_BACK_MIN_AGE_DAYS=0
ENV DECKMOVER_MOVE_BACK_SIDECARS=true
ENV DECKMOVER_TRIM_PLAN=true
ENV DECKMOVER_ONDECK=true
ENV PLEX_LIBRARIES="Movies,TV Shows"
ENV PUID=99
ENV PGID=100
ENV DECKMOVER_WEBUI_PORT=8080

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

ARG VERSION=dev
ARG VCS_REF=local
ARG BUILD_DATE
LABEL org.opencontainers.image.title="DeckMover" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/stormshaker/deckmover"
