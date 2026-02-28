import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


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
    spec: dict
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
            "spec": self.spec,
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

    tunnel_approval_mode:
      "manual"   — every tunnel request goes to a pending queue for approval.
      "auto"     — all tunnel requests from known peers are auto-approved.
      "per_peer" — use per-peer override; fall back to manual.

    Resource quota fields (all enforced on inbound RemoteApp submissions):
      max_cpu_per_pod      — max CPU cores a single pod may request (0 = unlimited, e.g. 0.5, 2)
      max_memory_mb_per_pod— max memory per pod in MiB (0 = unlimited)
      max_replicas_per_app — max replica count for any single RemoteApp (0 = unlimited)
      max_total_deployments— max concurrent RemoteApp deployments allowed (0 = unlimited)
      max_total_cpu        — max total CPU across all running RemoteApps (0 = unlimited)
      max_total_memory_mb  — max total memory across all running RemoteApps in MiB (0 = unlimited)
    """
    tunnel_approval_mode: Literal["manual", "auto", "per_peer"] = "manual"
    allow_inbound_remoteapps: bool = True
    allow_inbound_tunnels: bool = True
    max_tunnels_per_peer: int = 0
    log_level: str = "INFO"

    # Per-pod resource limits
    max_cpu_per_pod: float = 0.0        # CPU cores (0 = unlimited)
    max_memory_mb_per_pod: int = 0      # MiB (0 = unlimited)
    max_replicas_per_app: int = 0       # replicas (0 = unlimited)

    # Aggregate limits across all RemoteApps running on this cluster
    max_total_deployments: int = 0      # concurrent deployments (0 = unlimited)
    max_total_cpu: float = 0.0          # total CPU cores (0 = unlimited)
    max_total_memory_mb: int = 0        # total MiB (0 = unlimited)

    def to_dict(self):
        return {
            "tunnel_approval_mode": self.tunnel_approval_mode,
            "allow_inbound_remoteapps": self.allow_inbound_remoteapps,
            "allow_inbound_tunnels": self.allow_inbound_tunnels,
            "max_tunnels_per_peer": self.max_tunnels_per_peer,
            "log_level": self.log_level,
            "max_cpu_per_pod": self.max_cpu_per_pod,
            "max_memory_mb_per_pod": self.max_memory_mb_per_pod,
            "max_replicas_per_app": self.max_replicas_per_app,
            "max_total_deployments": self.max_total_deployments,
            "max_total_cpu": self.max_total_cpu,
            "max_total_memory_mb": self.max_total_memory_mb,
        }
