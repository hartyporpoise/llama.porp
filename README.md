<p align="center">
  <img src="static/logo.png" width="72" alt="Porpulsion logo" />
</p>

<h1 align="center">Porpulsion</h1>

<p align="center">
  Peer-to-peer Kubernetes connector. Deploy workloads across clusters over mutual TLS — no VPN, no service mesh, no central control plane.
</p>

---

```
┌──────────────────────┐                      ┌──────────────────────┐
│  Cluster A           │                      │  Cluster B           │
│  ┌────────────────┐  │  persistent WebSocket │  ┌────────────────┐  │
│  │   porpulsion   │◄─┼──────────────────────┼─►│   porpulsion   │  │
│  │  :8000         │  │  RemoteApp deploy    │  │  :8000         │  │
│  │  UI + WS + API │  │  status callbacks    │  │  UI + WS + API │  │
│  └────────────────┘  │  HTTP proxy tunnel   │  └────────────────┘  │
└──────────────────────┘                      └──────────────────────┘
```

## How it works

Each cluster runs one porpulsion agent. Agents exchange self-signed CA certificates during a one-time peering handshake, authenticated by a single-use invite token. Everything happens over plain HTTP/WebSocket on port 8000 — no separate mTLS port needed.

After peering, each agent opens a **persistent WebSocket channel** to its peer on port 8000. All subsequent inter-agent traffic — RemoteApp submissions, status callbacks, HTTP proxy tunnels — flows over this single long-lived connection. No new outbound connections are made per request. If the channel drops, both sides reconnect automatically with exponential backoff.

State (peers, submitted apps, settings) is persisted to a Kubernetes Secret and ConfigMap so restarts are transparent.

---

## Prerequisites

- Docker + Docker Compose (local dev)
- Kubernetes + Helm 3 (production)
- No local `kubectl` needed for local dev — all commands run via `docker exec`

---

## Local Development

```sh
git clone https://github.com/hartyporpoise/porpulsion
cd porpulsion
make deploy
```

`make deploy` does everything from scratch:

1. Starts two k3s clusters and a Helm runner container via docker-compose
2. Builds the `porpulsion-agent:local` image
3. Loads it into both clusters (no registry needed)
4. Helm-installs porpulsion into both clusters with NodePort services
5. Agents are ready to peer

| URL | Description |
|-----|-------------|
| `http://localhost:8001` | Cluster A dashboard |
| `http://localhost:8002` | Cluster B dashboard |

### Makefile targets

```sh
make deploy    # Full deploy from scratch (start clusters, build, helm install)
make redeploy  # Rebuild image + helm upgrade (clusters keep running)
make teardown  # Destroy everything (docker-compose down -v)
make status    # Show pods and peer status for both clusters
make logs      # Tail live agent logs from both clusters
make clean-ns  # Remove porpulsion namespace from both clusters
```

---

## Production Install

```sh
helm upgrade --install porpulsion oci://ghcr.io/hartyporpoise/porpulsion \
  --create-namespace \
  --namespace porpulsion \
  --set agent.agentName=my-cluster \
  --set agent.selfUrl=https://porpulsion.example.com
```

The agent runs two servers on separate ports:

| Port | Purpose | Exposure |
|------|---------|----------|
| **8000** | Dashboard UI + local management API | Internal only — never expose via Ingress |
| **8001** | Peer handshake (`/peer`) + WebSocket channel (`/ws`) | Expose via Ingress |

```sh
# Access the dashboard locally
kubectl port-forward svc/porpulsion 8000:8000 -n porpulsion
```

### nginx Ingress example

Only port 8001 (the peer-facing server) is exposed. The dashboard stays internal.

Two annotations are required:

- **`websocket-services`** — tells the ingress controller to proxy the WebSocket upgrade correctly (sets `proxy_http_version 1.1` and the `Upgrade`/`Connection` headers)
- **`proxy-read-timeout` / `proxy-send-timeout`** — must be longer than the agent's ping interval (20s); the default 60s will cause the persistent channel to drop during quiet periods

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: porpulsion
  namespace: porpulsion
  annotations:
    # Required: allows the WS upgrade to pass through nginx correctly.
    nginx.ingress.kubernetes.io/websocket-services: "porpulsion"
    # Keep the persistent WebSocket channel alive during quiet periods.
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - porpulsion.example.com
      secretName: porpulsion-tls   # your TLS cert (e.g. cert-manager / Let's Encrypt)
  rules:
    - host: porpulsion.example.com
      http:
        paths:
          - path: /peer
            pathType: Prefix
            backend:
              service:
                name: porpulsion
                port:
                  number: 8001
          - path: /ws
            pathType: Prefix
            backend:
              service:
                name: porpulsion
                port:
                  number: 8001
```

> **Encryption**: set `agent.selfUrl` to `https://porpulsion.example.com` and all peer WebSocket channels will use `wss://` (TLS via nginx). If `selfUrl` is `http://` the channel falls back to unencrypted `ws://` — the dashboard shows a yellow **live** badge as a warning.

Set `agent.selfUrl` to `https://porpulsion.example.com` in your Helm values.

### Helm values

| Value | Default | Description |
|-------|---------|-------------|
| `agent.agentName` | `""` | Human-readable cluster name shown in the dashboard |
| `agent.selfUrl` | `""` | Externally reachable URL for this agent. Peers use it for the initial invite handshake and the persistent WebSocket channel. Set to the nginx HTTPS hostname, e.g. `https://porpulsion.example.com`. Auto-detected if unset. |
| `agent.image` | `porpulsion-agent:latest` | Container image |
| `agent.pullPolicy` | `IfNotPresent` | Image pull policy |
| `namespace` | `porpulsion` | Namespace for the agent and all RemoteApp workloads |
| `service.type` | `ClusterIP` | Service type — use `NodePort` for local dev |
| `service.port` | `8000` | Dashboard UI and local management API (internal only) |
| `service.uiNodePort` | `""` | NodePort for dashboard (only when `type=NodePort`) |
| `service.peerPort` | `8001` | Peer handshake + WebSocket channel (expose via Ingress) |
| `service.peerNodePort` | `""` | NodePort for peer server (only when `type=NodePort`) |

---

## Settings

Managed per-agent from the **Settings** page in the dashboard.

### Access control

| Setting | Default | Description |
|---------|---------|-------------|
| Allow inbound workloads | `true` | Accept RemoteApp submissions from peers |
| Require manual approval | `false` | Queue inbound apps for approval before executing |
| Allowed image prefixes | _(empty)_ | Comma-separated list; empty = allow all |
| Blocked image prefixes | _(empty)_ | Always rejected regardless of allowed list |
| Allowed source peers | _(empty)_ | Comma-separated peer names; empty = all connected |

### Resource quotas

Enforced on **inbound** workloads at receive time. All CPU/memory values are k8s quantity strings.

| Setting | Description |
|---------|-------------|
| Require resource requests | Reject apps that omit `resources.requests.cpu` or `.memory` |
| Require resource limits | Reject apps that omit `resources.limits.cpu` or `.memory` |
| Max CPU request per pod | e.g. `500m` |
| Max CPU limit per pod | e.g. `1` |
| Max memory request per pod | e.g. `128Mi` |
| Max memory limit per pod | e.g. `256Mi` |
| Max replicas per app | Integer; `0` = unlimited |
| Max concurrent deployments | Integer; `0` = unlimited |
| Max total pods | Integer; `0` = unlimited |
| Max total CPU requests | e.g. `8` (aggregate across all running apps) |
| Max total memory requests | e.g. `32Gi` (aggregate across all running apps) |

---

## Usage

### 1 · Peer two clusters

Open the dashboard on Cluster A and navigate to **Peers**. Copy the invite token and CA fingerprint. On Cluster B, paste them into the **Connect a New Peer** form. Both sides will show the peer as connected within a few seconds.

Peers persist across restarts — the CA cert is stored in the `porpulsion-credentials` Secret. The WebSocket channel reconnects automatically on restart with exponential backoff starting at 2s.

### 2 · Deploy a RemoteApp

On the **Overview** page, enter an app name and fill in the YAML spec, then click **Deploy to Peer**.

```yaml
image: nginx:latest
replicas: 2
ports:
  - port: 80
    name: http
resources:
  requests:
    cpu: 250m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 256Mi
```

The spec is forwarded to the peer cluster over the WebSocket channel, which creates a Kubernetes Deployment in the `porpulsion` namespace. Status reflects back automatically (`Pending` → `Running`).

### 3 · Access via HTTP proxy

Navigate to the **Proxy** page to see all submitted apps with per-port proxy URLs. Click a URL to open the app through the WebSocket tunnel — no additional ports need to be exposed on the executing cluster.

Proxy URL format: `http://<dashboard>/remoteapp/<id>/proxy/<port>/`

---

## RemoteApp Spec

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | string | **required** | Container image, e.g. `nginx:latest` |
| `replicas` | integer | `1` | Pod replica count |
| `ports` | list | `[]` | Ports to expose via HTTP proxy. Each entry: `port` (required), `name` (optional) |
| `resources` | object | — | Kubernetes resource requests and limits. Contains `requests` and/or `limits` with `cpu` (e.g. `250m`, `1`) and `memory` (e.g. `128Mi`, `2Gi`) quantity strings |
| `command` | list | — | Override container ENTRYPOINT, e.g. `["/bin/sh", "-c"]` |
| `args` | list | — | Override container CMD / arguments |
| `env` | list | — | Environment variables. Each entry: `name` + `value`, or `valueFrom.secretKeyRef` / `valueFrom.configMapKeyRef` |
| `imagePullPolicy` | string | `IfNotPresent` | `Always`, `IfNotPresent`, or `Never` |
| `imagePullSecrets` | list | — | Names of k8s Secrets containing registry credentials |
| `readinessProbe` | object | — | `httpGet` (`path`, `port`) or `exec` (`command`), plus `initialDelaySeconds`, `periodSeconds`, `failureThreshold` |
| `securityContext` | object | — | `runAsNonRoot`, `runAsUser`, `runAsGroup`, `fsGroup`, `readOnlyRootFilesystem` |

---

## Architecture

```
porpulsion/
├── porpulsion/
│   ├── agent.py              # Flask app, startup, mTLS server lifecycle
│   ├── state.py              # Shared in-memory state (peers, apps, settings)
│   ├── models.py             # Peer, RemoteApp, AgentSettings dataclasses
│   ├── peering.py            # mTLS cert exchange, peer verification
│   ├── channel.py            # Persistent WebSocket channel (send/recv, reconnect)
│   ├── channel_handlers.py   # Message handlers (remoteapp/*, proxy/*, peer/*)
│   ├── tls.py                # CA/leaf cert generation, k8s Secret/ConfigMap persistence
│   ├── routes/
│   │   ├── peers.py          # /api/peers, /api/peer, /api/peers/connect, /api/token
│   │   ├── workloads.py      # /api/remoteapp, /api/remoteapps, /api/remoteapp/<id>/*
│   │   ├── tunnels.py       # /api/remoteapp/<id>/proxy/* (HTTP reverse proxy)
│   │   ├── settings.py      # /api/settings
│   │   ├── logs.py          # /api/logs
│   │   ├── ui.py            # UI at root: /, /peers, /workloads, /tunnels, /logs, /settings, /docs
│   │   └── ws.py            # /ws (WebSocket upgrade + CA auth)
│   └── k8s/
│       ├── executor.py       # Creates/updates/deletes Kubernetes Deployments
│       └── tunnel.py         # Resolves pod IP from labels, proxies HTTP
├── templates/
│   ├── base.html             # Layout, nav, theme; all pages extend this
│   ├── ui/                   # Page templates (overview, peers, workloads, tunnels, logs, settings, docs)
│   └── macros/               # Shared Jinja2 macros (cards, badges)
├── static/
│   ├── css/app.css           # Mobile-first layout, light/dark theme
│   ├── js/api.js              # API client (/api/* endpoints)
│   ├── js/app.js              # Toast, theme, DOM helpers; builds window.Porpulsion
│   ├── js/pages.js            # Page refresh, render, form bindings
│   └── logo.png
├── charts/porpulsion/        # Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml
│       ├── service.yaml
│       ├── role.yaml
│       ├── rolebinding.yaml
│       ├── clusterrole.yaml
│       ├── clusterrolebinding.yaml
│       ├── serviceaccount.yaml
│       └── secret.yaml
├── Dockerfile
├── docker-compose.yml
├── Makefile
└── requirements.txt
```

### WebSocket channel

After peering completes, each agent opens a persistent WebSocket connection to its peer's `/ws` endpoint. Authentication uses the CA fingerprint sent in the `X-Agent-Ca` header (base64-encoded PEM) — no client certificate is needed for the WS upgrade, which avoids nginx client-cert-forwarding complexity.

Both sides attempt to connect outbound on startup. Whichever side connects first becomes the active channel; the other side's outbound attempt arrives as an inbound connection and replaces it cleanly. The channel reconnects automatically with exponential backoff (2s → 4s → 8s → 16s → 30s); backoff resets to 2s after each successful connection.

All peer-to-peer messages are framed as JSON:

| Frame | Format | Description |
|-------|--------|-------------|
| Request | `{"id":"<hex>","type":"<method>","payload":{}}` | Expects a reply |
| Reply | `{"id":"<same>","type":"reply","ok":true,"payload":{}}` | Response to a request |
| Push | `{"type":"<event>","payload":{}}` | Fire-and-forget |

### State persistence

| Data | Store | Notes |
|------|-------|-------|
| CA cert + invite token | `porpulsion-credentials` Secret | Generated once, reused on restart |
| Peers (name, URL, CA cert) | `porpulsion-credentials` Secret | Written on every peer add/remove |
| Submitted apps | `porpulsion-state` ConfigMap | Written on create, status update, delete |
| Pending approval queue | `porpulsion-state` ConfigMap | Written on enqueue, approve, and reject |
| Settings | `porpulsion-state` ConfigMap | Written on every settings change |
| Executing apps | Reconstructed from k8s Deployments | Labels: `porpulsion.io/remote-app-id` |

### Security model

- Every agent generates a private CA on first boot. The CA cert is what peers exchange — never the private key.
- The peering handshake is bootstrapped over plain HTTPS (verify=False) with a single-use invite token. The CA fingerprint is pinned by the connecting operator before peering completes, preventing MITM.
- WebSocket connections authenticate by CA fingerprint — the connecting peer sends its CA PEM (base64-encoded) in the `X-Agent-Ca` header, verified against all known peer CAs.
- Invite tokens are single-use and rotated after every successful peering handshake.
- The HTTP proxy only routes to pods labelled `porpulsion.io/remote-app-id` — it cannot reach arbitrary pods.
- RBAC is scoped to the `porpulsion` namespace with only the permissions needed (Deployments, Services, the credentials Secret, the state ConfigMap).

---

## Roadmap

- [ ] `kubectl apply -f` support via CRD controller
- [ ] CLI (`porpulsion peer add`, `porpulsion app deploy`)
- [ ] Multi-peer routing with target cluster selector
- [ ] Leaf cert rotation without re-peering
- [ ] HA mode with leader election
