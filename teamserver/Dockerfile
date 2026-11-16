# Marketing Studio team server. One container = one team; its data lives in the /data volume.
#
# Build from the repository root (the build context must hold teamserver/ and VERSION):
#   docker build -f teamserver/Dockerfile -t marketingstudio-teamserver .
# or let teamserver/docker-compose.yml build it. See teamserver/README.md.
#
# No secrets are baked in: SMTP settings come from the environment at run time.

FROM python:3.12.7-slim-bookworm

# tini: a proper PID 1, so `docker stop` reaches the server and it shuts down cleanly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# A fixed, non-root user (uid 10001). A bind-mounted backups folder must be writable by this uid.
RUN groupadd --system --gid 10001 team \
    && useradd --system --uid 10001 --gid team --home-dir /home/team --create-home --shell /usr/sbin/nologin team \
    && mkdir -p /data /backups \
    && chown team:team /data /backups \
    && chmod 700 /data

WORKDIR /app
# The server needs only teamserver/ (standard library only) and VERSION (the version it reports).
# Tests and caches are left out by .dockerignore.
COPY --chown=root:root VERSION LICENSE ./
COPY --chown=root:root teamserver ./teamserver

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TEAMSERVER_DATA=/data \
    TEAMSERVER_PORT=8790

USER team
VOLUME ["/data"]
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8790/api/v1/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
# Port 8790 here, in HEALTHCHECK above and in docker-compose.yml / Caddyfile: change all of them together.
# --host 0.0.0.0: reachable from the proxy container (the port is not published to the host by docker-compose.yml).
# --trust-proxy: rate limits use the client address the proxy forwards. Only correct behind your own proxy.
CMD ["python3", "-m", "teamserver", "serve", "--host", "0.0.0.0", "--port", "8790", "--data", "/data", "--trust-proxy"]
