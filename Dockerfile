FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TG_GUARD_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 guard \
    && useradd --uid 1000 --gid 1000 --no-create-home guard \
    && mkdir /data && chown guard:guard /data
COPY --chmod=0644 *.py config.example.json ./
USER guard

FROM base AS test
COPY --chmod=0755 tests/ ./tests/
COPY --chmod=0755 start.sh ./start.sh
COPY --chmod=0755 scripts/Build-Release.py ./scripts/Build-Release.py
COPY --chmod=0755 scripts/Install-Docker.sh ./scripts/Install-Docker.sh
COPY --chmod=0755 install.sh ./install.sh
COPY --chmod=0644 compose.prebuilt.yaml ./compose.prebuilt.yaml
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM base AS runtime
CMD ["python", "docker_watchdog.py"]
