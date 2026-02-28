import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class EnvVarSource:
    secretKeyRef: dict | None = None    # {"name": str, "key": str}
    configMapKeyRef: dict | None = None  # {"name": str, "key": str}

    @classmethod
    def from_dict(cls, d: dict) -> "EnvVarSource":
        return cls(
            secretKeyRef=d.get("secretKeyRef"),
            configMapKeyRef=d.get("configMapKeyRef"),
        )

    def to_dict(self) -> dict:
        out: dict = {}
        if self.secretKeyRef:
            out["secretKeyRef"] = self.secretKeyRef
        if self.configMapKeyRef:
            out["configMapKeyRef"] = self.configMapKeyRef
        return out


@dataclass
class EnvVar:
    name: str
    value: str = ""
    valueFrom: EnvVarSource | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "EnvVar":
        vf = d.get("valueFrom")
        return cls(
            name=d["name"],
            value=str(d.get("value", "")),
            valueFrom=EnvVarSource.from_dict(vf) if vf else None,
        )

    def to_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.valueFrom:
            out["valueFrom"] = self.valueFrom.to_dict()
        else:
            out["value"] = self.value
        return out


@dataclass
class PortSpec:
    port: int
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "PortSpec":
        return cls(port=int(d["port"]), name=str(d.get("name", "")))

    def to_dict(self) -> dict:
        out: dict = {"port": self.port}
        if self.name:
            out["name"] = self.name
        return out


@dataclass
class ResourceRequirements:
    """
    Kubernetes-native resource requests and limits.

    Values are raw k8s quantity strings:
      cpu    — e.g. "250m" (250 millicores), "0.5", "1"
      memory — e.g. "64Mi", "1Gi", "512M"

    Either requests or limits (or both) may be omitted.
    """
    requests: dict[str, str] = field(default_factory=dict)  # {"cpu": "250m", "memory": "64Mi"}
    limits: dict[str, str] = field(default_factory=dict)    # {"cpu": "500m", "memory": "128Mi"}

    @classmethod
    def from_dict(cls, d: dict) -> "ResourceRequirements":
        return cls(
            requests=dict(d.get("requests") or {}),
            limits=dict(d.get("limits") or {}),
        )

    def to_dict(self) -> dict:
        out: dict = {}
        if self.requests:
            out["requests"] = self.requests
        if self.limits:
            out["limits"] = self.limits
        return out

    def is_empty(self) -> bool:
        return not self.requests and not self.limits


@dataclass
class ReadinessProbe:
    httpGet: dict | None = None    # {"path": str, "port": int}
    exec: dict | None = None       # {"command": [str]}
    initialDelaySeconds: int = 5
    periodSeconds: int = 10
    failureThreshold: int = 3

    @classmethod
    def from_dict(cls, d: dict) -> "ReadinessProbe":
        return cls(
            httpGet=d.get("httpGet"),
            exec=d.get("exec"),
            initialDelaySeconds=int(d.get("initialDelaySeconds", 5)),
            periodSeconds=int(d.get("periodSeconds", 10)),
            failureThreshold=int(d.get("failureThreshold", 3)),
        )

    def to_dict(self) -> dict:
        out: dict = {
            "initialDelaySeconds": self.initialDelaySeconds,
            "periodSeconds": self.periodSeconds,
            "failureThreshold": self.failureThreshold,
        }
        if self.httpGet:
            out["httpGet"] = self.httpGet
        if self.exec:
            out["exec"] = self.exec
        return out


@dataclass
class SecurityContext:
    runAsNonRoot: bool | None = None
    runAsUser: int | None = None
    runAsGroup: int | None = None
    fsGroup: int | None = None
    readOnlyRootFilesystem: bool | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "SecurityContext":
        return cls(
            runAsNonRoot=d.get("runAsNonRoot"),
            runAsUser=d.get("runAsUser"),
            runAsGroup=d.get("runAsGroup"),
            fsGroup=d.get("fsGroup"),
            readOnlyRootFilesystem=d.get("readOnlyRootFilesystem"),
        )

    def to_dict(self) -> dict:
        out: dict = {}
        for k in ("runAsNonRoot", "runAsUser", "runAsGroup", "fsGroup", "readOnlyRootFilesystem"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        return out


@dataclass
class RemoteAppSpec:
    """
    Typed schema for a RemoteApp spec. This is the authoritative model
    for what fields are accepted, their types, and their defaults.

    Required:
      image — container image to run

    Compute:
      replicas  — number of pod replicas (default 1)
      resources — Kubernetes resource requests and limits (cpu/memory quantity strings)

    Networking:
      port          — single container port shorthand (legacy)
      ports         — list of PortSpec (preferred)

    Entrypoint:
      command       — override container ENTRYPOINT
      args          — override container CMD / arguments

    Environment:
      env           — list of EnvVar (plain value or valueFrom secret/configmap)

    Image pull:
      imagePullPolicy   — Always | IfNotPresent | Never (default: IfNotPresent)
      imagePullSecrets  — list of Secret names with registry credentials

    Health:
      readinessProbe — ReadinessProbe (httpGet or exec)

    Security:
      securityContext — SecurityContext (pod-level + container-level fields)
    """
    # Required
    image: str = "nginx:latest"

    # Compute
    replicas: int = 1
    resources: ResourceRequirements = field(default_factory=ResourceRequirements)

    # Networking
    port: int | None = None
    ports: list[PortSpec] = field(default_factory=list)

    # Entrypoint
    command: list[str] | None = None
    args: list[str] | None = None

    # Environment
    env: list[EnvVar] = field(default_factory=list)

    # Image pull
    imagePullPolicy: Literal["Always", "IfNotPresent", "Never"] = "IfNotPresent"
    imagePullSecrets: list[str] = field(default_factory=list)

    # Health
    readinessProbe: ReadinessProbe | None = None

    # Security
    securityContext: SecurityContext | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "RemoteAppSpec":
        """Parse a raw spec dict (e.g. from JSON/YAML) into a typed RemoteAppSpec."""
        if not isinstance(d, dict):
            d = {}
        ports_raw = d.get("ports")
        env_raw = d.get("env")
        rp_raw = d.get("readinessProbe")
        sc_raw = d.get("securityContext")
        pull_secrets = d.get("imagePullSecrets")
        res_raw = d.get("resources")
        return cls(
            image=str(d.get("image", "nginx:latest")),
            replicas=max(1, int(d.get("replicas") or 1)),
            resources=ResourceRequirements.from_dict(res_raw)
                      if res_raw and isinstance(res_raw, dict) else ResourceRequirements(),
            port=int(d["port"]) if d.get("port") else None,
            ports=[PortSpec.from_dict(p) for p in ports_raw if p.get("port")]
                  if ports_raw and isinstance(ports_raw, list) else [],
            command=list(d["command"]) if d.get("command") else None,
            args=list(d["args"]) if d.get("args") else None,
            env=[EnvVar.from_dict(e) for e in env_raw if e.get("name")]
                if env_raw and isinstance(env_raw, list) else [],
            imagePullPolicy=d.get("imagePullPolicy", "IfNotPresent"),
            imagePullSecrets=list(pull_secrets)
                             if pull_secrets and isinstance(pull_secrets, list) else [],
            readinessProbe=ReadinessProbe.from_dict(rp_raw)
                           if rp_raw and isinstance(rp_raw, dict) else None,
            securityContext=SecurityContext.from_dict(sc_raw)
                            if sc_raw and isinstance(sc_raw, dict) else None,
        )

    def to_dict(self) -> dict:
        """Serialize back to a plain dict (for JSON API responses and persistence)."""
        out: dict[str, Any] = {"image": self.image, "replicas": self.replicas}
        if not self.resources.is_empty():
            out["resources"] = self.resources.to_dict()
        if self.port is not None:
            out["port"] = self.port
        if self.ports:
            out["ports"] = [p.to_dict() for p in self.ports]
        if self.command:
            out["command"] = self.command
        if self.args:
            out["args"] = self.args
        if self.env:
            out["env"] = [e.to_dict() for e in self.env]
        if self.imagePullPolicy != "IfNotPresent":
            out["imagePullPolicy"] = self.imagePullPolicy
        if self.imagePullSecrets:
            out["imagePullSecrets"] = self.imagePullSecrets
        if self.readinessProbe:
            out["readinessProbe"] = self.readinessProbe.to_dict()
        if self.securityContext:
            out["securityContext"] = self.securityContext.to_dict()
        return out


@dataclass
class Peer:
    name: str
    url: str
    ca_pem: str = ""  # PEM CA cert received from this peer during handshake (internal only)
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {"name": self.name, "url": self.url, "connected_at": self.connected_at}


@dataclass
class RemoteApp:
    name: str
    spec: RemoteAppSpec
    source_peer: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "Pending"
    target_peer: str = ""   # peer this app was submitted to (set on the submitting side)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "spec": self.spec.to_dict() if isinstance(self.spec, RemoteAppSpec) else self.spec,
            "source_peer": self.source_peer,
            "target_peer": self.target_peer,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class TunnelRequest:
    """A pending tunnel request from a peer, waiting for local approval."""
    id: str
    peer_name: str
    remote_app_id: str
    target_port: int
    status: Literal["pending", "approved", "rejected"] = "pending"
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "id": self.id,
            "peer_name": self.peer_name,
            "remote_app_id": self.remote_app_id,
            "target_port": self.target_port,
            "status": self.status,
            "requested_at": self.requested_at,
        }


@dataclass
class AgentSettings:
    """
    Persistent (in-memory) settings for this agent.

    Access control:
      allow_inbound_remoteapps   — accept RemoteApp submissions from peers
      require_remoteapp_approval — queue inbound apps for manual approval before executing
      allowed_images             — comma-separated image prefixes; empty = allow all
      blocked_images             — comma-separated image prefixes always rejected
      allowed_source_peers       — comma-separated peer names that may submit; empty = all connected
      require_resource_limits    — reject apps that omit resources.requests

    Resource quotas (enforced on inbound RemoteApp submissions).
    All cpu/memory values are k8s quantity strings, e.g. "500m", "1", "256Mi", "2Gi".
    Empty string = unlimited.

      Per-pod:
        max_cpu_request_per_pod    — max cpu request per pod
        max_cpu_limit_per_pod      — max cpu limit per pod
        max_memory_request_per_pod — max memory request per pod
        max_memory_limit_per_pod   — max memory limit per pod
        max_replicas_per_app       — max replicas for a single app (0 = unlimited)

      Aggregate:
        max_total_deployments      — max concurrent RemoteApp deployments (0 = unlimited)
        max_total_pods             — max total pods across all deployments (0 = unlimited)
        max_total_cpu_requests     — max total cpu requests across all running apps
        max_total_memory_requests  — max total memory requests across all running apps
    """
    # Access control
    allow_inbound_remoteapps: bool = True
    require_remoteapp_approval: bool = False
    allowed_images: str = ""            # comma-separated prefixes; empty = allow all
    blocked_images: str = ""            # comma-separated prefixes; always denied
    allowed_source_peers: str = ""      # comma-separated peer names; empty = all connected
    require_resource_limits: bool = False

    # Tunnel control
    allow_inbound_tunnels: bool = True
    tunnel_approval_mode: Literal["manual", "auto", "per_peer"] = "auto"
    allowed_tunnel_peers: str = ""      # comma-separated peer names allowed to open tunnels; empty = all connected

    # Diagnostics
    log_level: str = "INFO"

    # Per-pod resource quotas (k8s quantity strings; "" = unlimited)
    max_cpu_request_per_pod: str = ""
    max_cpu_limit_per_pod: str = ""
    max_memory_request_per_pod: str = ""
    max_memory_limit_per_pod: str = ""
    max_replicas_per_app: int = 0

    # Aggregate quotas
    max_total_deployments: int = 0
    max_total_pods: int = 0
    max_total_cpu_requests: str = ""
    max_total_memory_requests: str = ""

    def to_dict(self):
        return {
            "allow_inbound_remoteapps": self.allow_inbound_remoteapps,
            "require_remoteapp_approval": self.require_remoteapp_approval,
            "allowed_images": self.allowed_images,
            "blocked_images": self.blocked_images,
            "allowed_source_peers": self.allowed_source_peers,
            "require_resource_limits": self.require_resource_limits,
            "allow_inbound_tunnels": self.allow_inbound_tunnels,
            "tunnel_approval_mode": self.tunnel_approval_mode,
            "allowed_tunnel_peers": self.allowed_tunnel_peers,
            "log_level": self.log_level,
            "max_cpu_request_per_pod": self.max_cpu_request_per_pod,
            "max_cpu_limit_per_pod": self.max_cpu_limit_per_pod,
            "max_memory_request_per_pod": self.max_memory_request_per_pod,
            "max_memory_limit_per_pod": self.max_memory_limit_per_pod,
            "max_replicas_per_app": self.max_replicas_per_app,
            "max_total_deployments": self.max_total_deployments,
            "max_total_pods": self.max_total_pods,
            "max_total_cpu_requests": self.max_total_cpu_requests,
            "max_total_memory_requests": self.max_total_memory_requests,
        }
