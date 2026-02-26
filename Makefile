# ─────────────────────────────────────────────────────────────────────────────
# Porpulsion Makefile
#
# Targets:
#   make build              — compile the porpulsion binary
#   make run                — build + run (requires Ollama running locally)
#   make docker-up          — build image and start porpulsion + Ollama via Compose
#   make docker-down        — stop Compose stack
#   make docker-build       — build Docker image only
#   make test               — run Go tests
#   make clean              — remove build artifacts
# ─────────────────────────────────────────────────────────────────────────────

BINARY       := porpulsion
GO           := go
OLLAMA_URL   ?= http://localhost:11434
DEFAULT_MODEL ?=
PORT         ?= 8080

.PHONY: all build run test lint clean docker-build docker-up docker-down tidy deps

# ── Default ───────────────────────────────────────────────────────────────────
all: build

# ── Build ─────────────────────────────────────────────────────────────────────
build:
	@echo ">>> Building porpulsion ..."
	CGO_ENABLED=0 $(GO) build \
		-ldflags="-s -w" \
		-o $(BINARY) \
		./cmd/porpulsion
	@echo ">>> Binary: ./$(BINARY)"

# ── Run (expects Ollama already running at OLLAMA_URL) ────────────────────────
run: build
	@echo ">>> Starting porpulsion (Ollama: $(OLLAMA_URL), port: $(PORT)) ..."
	./$(BINARY) serve \
		--ollama-url "$(OLLAMA_URL)" \
		--model "$(DEFAULT_MODEL)" \
		--port $(PORT)

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	$(GO) test ./... -v -timeout 60s

# ── Lint ──────────────────────────────────────────────────────────────────────
lint:
	@which golangci-lint > /dev/null || \
		(echo "Install: https://golangci-lint.run" && exit 1)
	golangci-lint run ./...

# ── Docker ────────────────────────────────────────────────────────────────────
docker-build:
	docker build -t porpulsion:latest .

docker-up:
	DEFAULT_MODEL=$(DEFAULT_MODEL) PORT=$(PORT) docker compose up --build

docker-down:
	docker compose down

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	rm -f $(BINARY)

tidy:
	$(GO) mod tidy

deps:
	$(GO) mod download