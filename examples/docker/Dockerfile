# Grok Gadgets gateway as a relay (hub ADR 009). Build from a checkout of this repository at
# the commit or release tag you want to run; see docs/relay-host.md.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY protocol ./protocol
# The locked dependencies, without development tools. No editable install.
RUN uv sync --locked --no-dev --no-editable \
    && useradd --create-home --uid 10001 gadgets \
    && mkdir -p /data/config /data/logs \
    && chown -R gadgets:gadgets /data

USER gadgets
# Config, CA, tokens and sign-ins live under /data/config; logs under /data/logs.
ENV PATH="/app/.venv/bin:$PATH" \
    XDG_CONFIG_HOME=/data/config \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8443
ENTRYPOINT ["grok-gadgets-gateway"]
CMD ["--help"]
