# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-trixie AS runtime
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    WHATSAPP_MCP_TRANSPORT=http \
    WHATSAPP_MCP_HOST=0.0.0.0 \
    WHATSAPP_MCP_PORT=8000 \
    WHATSAPP_API_URL=http://bridge:8080/api \
    WHATSAPP_DB_PATH=/app/store/messages.db \
    WHATSMEOW_DB_PATH=/app/store/whatsapp.db
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --home-dir /home/app app
COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project \
 && chown -R app:app /app
COPY --chown=app:app . .
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:${WHATSAPP_MCP_PORT}/healthz || exit 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","main.py"]
