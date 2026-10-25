# Gatewright solo server. Data (database, demo fixtures) lives in the /data volume, never in the image.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY gw ./gw
COPY schemas ./schemas
COPY examples ./examples
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra graph && useradd --system --uid 10001 --home /data gw \
    && mkdir -p /data && chown gw /data
ENV GW_DB=/data/gw.duckdb PATH="/app/.venv/bin:$PATH" LOCALAPPDATA=/data/local
USER gw
VOLUME ["/data"]
EXPOSE 8000
# Binding 0.0.0.0 is refused until a user has a password (login mode): gw user password <short>
# Behind a TLS reverse proxy set GW_FORWARDED_ALLOW_IPS to the proxy's address and GW_PUBLIC_HTTPS=1.
CMD ["gw", "serve", "--host", "0.0.0.0", "--port", "8000"]
