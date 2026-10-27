# meertex-server: the web app, its API and the live documents' sync endpoint,
# in one process. The shape is meerpad's. git is installed because every
# project's history is a git repository (docs/DESIGN.md §9).
FROM python:3.14-slim

ARG MEERTEX_VERSION=0.0.0

LABEL org.opencontainers.image.title="meertex-server" \
      org.opencontainers.image.description="meertex: write LaTeX papers locally, synced live, with deadlines and versions." \
      org.opencontainers.image.source="https://github.com/ribalba/meertex" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${MEERTEX_VERSION}"

# Project histories and blobs live under /data, which compose mounts a named
# volume over. The database holds everything else, so /data plus the database
# volume is the whole install.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the world.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY VERSION /app/VERSION
# The build's own number wins over whatever the tree happened to hold, so an
# image cannot claim a version it was not built from (meerpic's rule).
RUN [ "$MEERTEX_VERSION" = "0.0.0" ] || printf '%s\n' "$MEERTEX_VERSION" > /app/VERSION

# An unprivileged user of its own. /data is made here, owned by that user,
# because a fresh named volume copies the ownership of the directory it is
# mounted over. No VOLUME instruction, compose names the volume.
RUN useradd --create-home --uid 10001 meertex \
 && mkdir -p /data/projects \
 && chown -R meertex /app /data
USER meertex

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# One worker, on purpose: the live documents are rooms held in memory, and a
# second worker would hold a second copy that never hears of the first's edits.
#
# --proxy-headers with every address trusted: in production the only thing
# that can reach port 8000 is the reverse proxy (Coolify's Traefik over the
# project network, or docker-compose.yml's 127.0.0.1 port).
#
# --ws-max-size: uvicorn refuses WebSocket messages over 16 MB by default. The
# first sync of a checkout is the whole document in one message, and the app
# checks MAX_SYNC_MESSAGE_BYTES (64 MB) itself, so uvicorn's cap sits above it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--ws-max-size", "100000000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
