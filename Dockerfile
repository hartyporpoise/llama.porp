# ─────────────────────────────────────────────────────────────────────────────
# Porpulsion — Dockerfile
#
# Pure Go build — no CGO needed now that inference is handled by Ollama.
# Result is a small static binary + distroless runtime image.
# ─────────────────────────────────────────────────────────────────────────────

FROM golang:1.22-bookworm AS builder

WORKDIR /app

# Download deps first (layer cache)
COPY go.mod go.sum ./
RUN go mod download

# Build — CGO disabled for a fully static binary
COPY . .
RUN CGO_ENABLED=0 go build \
      -ldflags="-s -w" \
      -o porpulsion \
      ./cmd/porpulsion

# ─── Runtime image ────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/porpulsion /usr/local/bin/porpulsion

EXPOSE 8080

ENTRYPOINT ["porpulsion", "serve"]
