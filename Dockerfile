FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 xagent && mkdir /data && chown xagent:xagent /data

COPY pyproject.toml README.md ./
COPY xagent ./xagent
RUN pip install .

COPY config ./config

USER xagent
ENV XAGENT_DATA_DIR=/data \
    XAGENT_CONFIG=/app/config/agent.yaml
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 CMD ["xagent", "health"]
CMD ["xagent", "run"]
