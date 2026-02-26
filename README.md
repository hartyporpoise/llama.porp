<p align="center">
  <img src="web/static/logo.png" width="72" alt="Porpulsion logo" />
</p>

<h1 align="center">Porpulsion</h1>

<p align="center">
  A fast, lightweight UI and proxy layer for <a href="https://ollama.com">Ollama</a>.<br/>
  OpenAI-compatible API · Real-time metrics · Runtime performance flags · Helm chart included.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#features">Features</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#api">API</a> ·
  <a href="#feature-flags">Feature flags</a> ·
  <a href="#kubernetes--helm">Kubernetes</a> ·
  <a href="#development">Development</a>
</p>

---

## Quick start

**Docker Compose (recommended)**

```bash
git clone https://github.com/hartyporpoise/porpulsion
cd porpulsion
docker compose up
```

Open [http://localhost:8080](http://localhost:8080).

**Local binary** (requires Ollama already running)

```bash
go build -o porpulsion ./cmd/porpulsion
./porpulsion serve --ollama-url http://localhost:11434
```

**With a default model**

```bash
DEFAULT_MODEL=llama3.2 docker compose up
# or
./porpulsion serve --model llama3.2
```

---

## Features

### Web UI
- **Chat interface** — streaming responses with markdown rendering (code highlighting, tables, lists)
- **Conversation history** — persisted in browser localStorage, sidebar navigation
- **Model picker** — switch models mid-session from the chat input bar
- **Server info panel** — CPU topology, SIMD feature badges, RAM, Ollama version
- **Live tok/s badge** — real-time tokens per second via SSE stream

### API
- **OpenAI-compatible** — drop-in replacement for `/v1/chat/completions` and `/v1/completions`
- **Model management** — pull, list, and delete models from the UI or API
- **Health endpoint** — `/health` checks both porpulsion and Ollama

### Performance feature flags
Toggled at runtime from the Settings page — no restart required.

**CPU Performance**

| Flag | Effect |
|---|---|
| Flash Attention | O(1) memory attention — faster at long context (≥ Ollama 0.1.33) |
| Memory-Map Weights | Load weights via OS page cache — instant cold start |
| Lock Weights in RAM | Pin weights in physical RAM — eliminates swap stalls |
| Thread Affinity Hint | Restrict inference to P-cores on Intel hybrid CPUs |
| Prompt Prefix Cache | Reuse Ollama KV context when conversation prefix matches |
| Request Batching | Serialise concurrent requests — higher throughput, higher latency |
| Quantization Advisor | Recommend the best quant tier for your available RAM |

**Memory Footprint**

| Flag | Effect |
|---|---|
| Lean Context Window | Cap KV context to 512 tokens — ~8× less KV cache RAM |
| Low VRAM Mode | Skip scratch buffers, use streaming attention — 15–25% less peak RAM |
| Aggressive Quantization | Pull one quant tier lower than recommended to fit larger models |

---

## Configuration

All settings can be passed as CLI flags or environment variables.

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--ollama-url` | `OLLAMA_URL` | `http://ollama:11434` | Ollama API base URL |
| `--model` / `-m` | — | _(none)_ | Default model pre-selected in UI |
| `--port` / `-p` | — | `8080` | HTTP listen port |
| `--host` | — | `0.0.0.0` | Bind address |

### Docker Compose environment variables

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_MODEL` | _(none)_ | Model to pre-select on startup |
| `PORT` | `8080` | Host port to expose |

---

## API

Porpulsion exposes an OpenAI-compatible API and its own management API.

### OpenAI-compatible

```
POST /v1/chat/completions    Chat completions (streaming + non-streaming)
POST /v1/completions         Text completions
GET  /v1/models              List available models
```

These endpoints are compatible with any OpenAI SDK — just point `base_url` at your porpulsion instance.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="unused")
resp = client.chat.completions.create(
    model="llama3.2",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in resp:
    print(chunk.choices[0].delta.content, end="", flush=True)
```

### Management API

```
GET  /health                  Health check (porpulsion + Ollama)
GET  /api/info                Server info: CPU, RAM, Ollama version, models
GET  /api/metrics             Metrics snapshot (or SSE stream with Accept: text/event-stream)
GET  /api/models              List local Ollama models
POST /api/pull                Pull a model (SSE progress stream)
DELETE /api/models/{name}     Delete a model
GET  /api/features            List all feature flags
POST /api/features            Toggle a feature flag
GET  /api/quant-advice        Quantization recommendation for this machine
```

**Toggle a feature flag**

```bash
curl -X POST http://localhost:8080/api/features \
  -H 'Content-Type: application/json' \
  -d '{"feature":"flash_attn","enabled":true}'
```

**Pull a model**

```bash
curl -X POST http://localhost:8080/api/pull \
  -H 'Content-Type: application/json' \
  -d '{"name":"llama3.2"}'
```

---

## Feature flags

Feature flags are in-memory only — they reset on restart. This is intentional: it makes A/B testing easy (restart with different defaults via env vars or config).

Available flag IDs:

```
flash_attn       mmap_weights     mlock_weights
lean_context     low_vram         aggressive_quant
batching         prefix_cache     quant_advisor     thread_hint
```

---

## Kubernetes / Helm

A Helm chart is included under `charts/porpulsion/`. Ollama runs as a sidecar container in the same pod as porpulsion, sharing `localhost` — no inter-pod networking needed.

**Install**

```bash
helm install porpulsion ./charts/porpulsion \
  --namespace porpulsion \
  --create-namespace
```

**Access the UI**

```bash
kubectl port-forward svc/porpulsion 8080:8080 -n porpulsion
# then open http://localhost:8080
```

**Pull a model**

```bash
kubectl exec -n porpulsion deploy/porpulsion -c ollama -- ollama pull llama3.2
```

**Expose via Ingress**

```bash
helm install porpulsion ./charts/porpulsion \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set "ingress.hosts[0].host=porpulsion.example.com" \
  --set "ingress.hosts[0].paths[0].path=/" \
  --set "ingress.hosts[0].paths[0].pathType=Prefix"
```

**Use an external Ollama instance**

```bash
helm install porpulsion ./charts/porpulsion \
  --set ollama.enabled=false \
  --set ollamaUrl=http://my-ollama-service:11434
```

**Key values**

| Value | Default | Description |
|---|---|---|
| `defaultModel` | `""` | Model pre-selected in UI |
| `ollama.enabled` | `true` | Run Ollama as a sidecar |
| `ollama.persistence.size` | `30Gi` | PVC size for model storage |
| `ollama.persistence.storageClass` | `""` | Storage class (cluster default) |
| `ollamaUrl` | `""` | External Ollama URL (when `ollama.enabled=false`) |
| `ingress.enabled` | `false` | Create an Ingress resource |
| `service.type` | `ClusterIP` | Kubernetes Service type |

The model PVC is annotated `helm.sh/resource-policy: keep` — downloaded models are preserved across `helm uninstall`.

---

## Development

**Prerequisites:** Go 1.22+, Docker (for compose)

```bash
# Build
make build

# Run locally (Ollama must be running)
make run

# Run tests
make test

# Docker Compose
make docker-up
make docker-down

# Lint (requires golangci-lint)
make lint
```

**Project layout**

```
cmd/porpulsion/        CLI entrypoint (cobra)
internal/
  api/                 HTTP server, all route handlers
  config/              Config struct
  cpu/                 CPU topology detection (cross-platform)
  features/            Runtime feature flags + batcher + prefix cache + quant advisor
  metrics/             Thread-safe tok/s / TTFT / TPOT collector
  ollama/              Typed Ollama HTTP client
web/
  static/              index.html, app.js, style.css, logo.png
  embed.go             //go:embed static
charts/porpulsion/     Helm chart
Dockerfile
docker-compose.yml
Makefile
```

---

## License

[MIT](LICENSE)