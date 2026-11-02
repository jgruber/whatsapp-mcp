# syntax=docker/dockerfile:1.7
FROM golang:1.26-trixie AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go mod download
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/whatsapp-bridge .

FROM debian:trixie-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --home-dir /home/app app \
 && install -d -m 0700 -o app -g app /app/store
COPY --from=build /out/whatsapp-bridge /usr/local/bin/whatsapp-bridge
WORKDIR /app
USER app
ENV WHATSAPP_BRIDGE_PORT=8080 \
    WHATSAPP_BRIDGE_BIND=0.0.0.0 \
    WHATSAPP_BRIDGE_ALLOWED_HOSTS=bridge:8080 \
    WHATSAPP_DB_PATH=/app/store/messages.db \
    WHATSMEOW_DB_PATH=/app/store/whatsapp.db \
    WHATSAPP_MEDIA_ROOTS=/app/store/outbox:/app/store/uploads
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m \
  CMD curl -fsS -H "Authorization: Bearer $(cat /app/store/.bridge-token 2>/dev/null)" \
      http://127.0.0.1:${WHATSAPP_BRIDGE_PORT}/api/health || exit 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/usr/local/bin/whatsapp-bridge"]
