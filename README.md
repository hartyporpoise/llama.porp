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
│  ┌────────────────┐  │  mTLS  ·  RemoteApp  │  ┌────────────────┐  │
│  │   porpulsion   │◄─┼──────────────────────┼─►│   porpulsion   │  │
│  │  UI   :8000    │  │  status reflection   │  │  UI   :8000    │  │
│  │  mTLS :8443    │  │  HTTP proxy tunnel   │  │  mTLS :8443    │  │
│  └────────────────┘  │                      │  └────────────────┘  │
└──────────────────────┘                      └──────────────────────┘
```

## How it works

Each cluster runs one porpulsion agent. Agents exchange self-signed CA certificates during a one-time peering handshake authenticated by a single-use invite token. After that, all inter-agent traffic uses mutual TLS — no shared secrets, no PKI infrastructure required.

Once peered, submit a **RemoteApp** spec from Cluster A. The agent forwards it to Cluster B, which creates a real Kubernetes Deployment. Status flows back automatically. An HTTP reverse proxy tunnels traffic to the remote pod over the same mTLS connection — no extra port exposure or ingress rules needed on the executing side.

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
| `https://localhost:8003` | Cluster A mTLS agent endpoint |
| `https://localhost:8004` | Cluster B mTLS agent endpoint |

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
  --set agent.selfUrl=https://agent.example.com:8443
```

Both services default to `ClusterIP`. Expose them with your own Ingress or LoadBalancer:

- **Dashboard** (HTTP, port 8000) — standard nginx Ingress, any path
- **mTLS agent** (TLS, port 8443) — nginx routes `/agent` to this port; peer agents communicate only on `/agent/*` paths so it stays cleanly separated from the dashboard

```sh
# Quick access without an Ingress
kubectl port-forward svc/porpulsion 8000:8000 -n porpulsion   # dashboard
kubectl port-forward svc/porpulsion 8443:8443 -n porpulsion   # mTLS agent
```

### nginx Ingress example

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: porpulsion
  namespace: porpulsion
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /$2
    # Pass the X-Invite-Token header through to the pod — nginx strips
    # unknown headers by default and the peering handshake depends on it.
    nginx.ingress.kubernetes.io/configuration-snippet: |
      proxy_pass_header X-Invite-Token;
spec:
  ingressClassName: nginx
  rules:
    - host: porpulsion.example.com
      http:
        paths:
          # mTLS agent — peer-to-peer traffic only, routed to port 8443.
          # Note: nginx terminates TLS here so this is not end-to-end mTLS.
          # For true mTLS, expose port 8443 via a LoadBalancer service and
          # point agent.selfUrl directly at it instead.
          - path: /agent(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: porpulsion
                port:
                  number: 8443
          # Dashboard — plain HTTP, serves the UI and API.
          - path: /()(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: porpulsion
                port:
                  number: 8000
```

> **Note:** Set `agent.selfUrl` to `https://porpulsion.example.com` (the ingress hostname) so peer agents know where to reach `/agent/*`. For true end-to-end mTLS, use a `LoadBalancer` service on port 8443 instead and set `selfUrl` to point at that.

### Helm values

| Value | Default | Description |
|-------|---------|-------------|
| `agent.agentName` | `""` | Human-readable cluster name shown in the dashboard |
| `agent.selfUrl` | `""` | Externally reachable mTLS URL, e.g. `https://agent.example.com:8443`. Auto-detected if unset. |
| `agent.image` | `porpulsion-agent:latest` | Container image |
| `agent.pullPolicy` | `IfNotPresent` | Image pull policy |
| `namespace` | `porpulsion` | Namespace for the agent and all RemoteApp workloads |
| `service.type` | `ClusterIP` | Service type — use `NodePort` for local dev |
| `service.uiPort` | `8000` | Dashboard port |
| `service.agentPort` | `8443` | mTLS agent port |
| `service.uiNodePort` | `""` | NodePort for dashboard (only when `type=NodePort`) |
| `service.agentNodePort` | `""` | NodePort for mTLS agent (only when `type=NodePort`) |

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

Peers persist across restarts — the CA cert is stored in the `porpulsion-credentials` Secret.

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

The spec is forwarded to the peer cluster, which creates a Kubernetes Deployment in the `porpulsion` namespace. Status reflects back automatically (`Pending` → `Running`).

### 3 · Access via HTTP proxy

Navigate to the **Proxy** page to see all submitted apps with per-port proxy URLs. Click a URL to open the app through the mTLS tunnel — no additional ports need to be exposed on the executing cluster.

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
│   ├── agent.py          # Flask app, startup, mTLS server lifecycle
│   ├── state.py          # Shared in-memory state (peers, apps, settings)
│   ├── models.py         # Peer, RemoteApp, AgentSettings dataclasses
│   ├── peering.py        # mTLS cert exchange, peer verification
│   ├── tls.py            # CA/leaf cert generation, k8s Secret/ConfigMap persistence
│   ├── routes/
│   │   ├── peers.py      # /peers, /peer, /peers/connect, /token
│   │   ├── workloads.py  # /remoteapp, /remoteapps, /remoteapp/<id>/*
│   │   ├── tunnels.py    # /remoteapp/<id>/proxy/* (HTTP reverse proxy)
│   │   └── settings.py   # /settings
│   └── k8s/
│       ├── executor.py   # Creates/updates/deletes Kubernetes Deployments
│       └── tunnel.py     # Resolves pod IP from labels, proxies HTTP
├── templates/
│   └── dashboard.html    # Single-page dashboard (no build step)
├── static/
│   └── logo.png
├── charts/porpulsion/    # Helm chart
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
└── requirements.txt      # flask, requests, kubernetes, cryptography
```

### State persistence

| Data | Store | Notes |
|------|-------|-------|
| TLS certs + invite token | `porpulsion-credentials` Secret | Generated once, reused on restart |
| Peers (name, URL, CA cert) | `porpulsion-credentials` Secret | Written on every peer add/remove |
| Submitted apps | `porpulsion-state` ConfigMap | Written on create, status update, delete |
| Pending approval queue | `porpulsion-state` ConfigMap | Written on enqueue, approve, and reject |
| Settings | `porpulsion-state` ConfigMap | Written on every settings change |
| Executing apps | Reconstructed from k8s Deployments | Labels: `porpulsion.io/remote-app-id` |

### Security model

- Every agent generates a private CA on first boot. The CA cert is what peers exchange — never the private key.
- All agent-to-agent calls use mTLS with `CERT_REQUIRED`. A peer without a cert signed by a trusted CA is rejected at the TLS layer.
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
