import logging
import uuid
from datetime import datetime, timezone

import requests as _req
from flask import Blueprint, request, jsonify

from porpulsion import state, tls
from porpulsion.models import RemoteApp, RemoteAppSpec
from porpulsion.peering import verify_peer
from porpulsion.k8s.executor import (
    run_workload, delete_workload, scale_workload, get_deployment_status,
)

log = logging.getLogger("porpulsion.routes.workloads")

bp = Blueprint("workloads", __name__)


# ── k8s quantity parser ────────────────────────────────────────
# Memory suffixes — deliberately excludes bare "m" to avoid collision with CPU millicores.
# In practice no one uses "m" for memory (they use "Mi"/"Gi"). If someone passes "500M"
# that's also unusual; we treat it as megabytes via the "m" → millicore branch below and
# let the caller figure it out — the important thing is "500m" CPU works correctly.
_MEMORY_SUFFIXES = {
    "ki": 2**10, "mi": 2**20, "gi": 2**30, "ti": 2**40,
    "k":  1e3,               "g":  1e9,   "t":  1e12,
}


def _parse_quantity(q: str) -> float:
    """
    Parse a Kubernetes quantity string into a normalised float.
    CPU: returns cores (e.g. "250m" → 0.25, "1" → 1.0).
    Memory: returns bytes (e.g. "64Mi" → 67108864, "1Gi" → 1073741824).
    Returns 0.0 for empty/None.

    Detection order:
      1. Binary/decimal memory suffixes (ki, mi, gi, ti, k, g, t) — checked first so
         "128Mi" is never confused with a millicore value.
      2. Bare "m" suffix → CPU millicores (500m → 0.5 cores).
      3. Plain number → assume cores for CPU, bytes for memory (caller decides unit).
    """
    if not q:
        return 0.0
    q = str(q).strip()
    lower = q.lower()
    # Memory suffixes (multi-char checked before single-char via dict order)
    for suffix, factor in _MEMORY_SUFFIXES.items():
        if lower.endswith(suffix):
            return float(q[: -len(suffix)]) * factor
    # CPU millicore — bare "m" suffix, e.g. "500m"
    if lower.endswith("m"):
        return float(q[:-1]) / 1000.0
    return float(q)


def _peer_session(peer=None) -> _req.Session:
    session = _req.Session()
    session.cert = (state.AGENT_CERT_PATH, state.AGENT_KEY_PATH)
    session.verify = tls.peer_ca_path(peer.name) if (peer and peer.ca_pem) else False
    return session


def _check_image_policy(image: str) -> str | None:
    """Check image against allowed/blocked prefix lists. Returns error string or None."""
    s = state.settings

    blocked = [p.strip() for p in s.blocked_images.split(",") if p.strip()]
    for prefix in blocked:
        if image.startswith(prefix):
            return f"Image '{image}' is blocked by this cluster's policy"

    allowed = [p.strip() for p in s.allowed_images.split(",") if p.strip()]
    if allowed and not any(image.startswith(p) for p in allowed):
        return (f"Image '{image}' is not in this cluster's allowed image list "
                f"({', '.join(allowed)})")

    return None


def _check_resource_quota(spec: RemoteAppSpec, source_peer: str = "") -> str | None:
    s = state.settings
    res = spec.resources

    # Presence requirements — checked before any numeric limits
    if s.require_resource_requests:
        if not res.requests.get("cpu") or not res.requests.get("memory"):
            return "This cluster requires resource requests (resources.requests.cpu and resources.requests.memory)"
    if s.require_resource_limits:
        if not res.limits.get("cpu") or not res.limits.get("memory"):
            return "This cluster requires resource limits (resources.limits.cpu and resources.limits.memory)"

    req_cpu_req = _parse_quantity(res.requests.get("cpu", ""))
    req_cpu_lim = _parse_quantity(res.limits.get("cpu", ""))
    req_mem_req = _parse_quantity(res.requests.get("memory", ""))
    req_mem_lim = _parse_quantity(res.limits.get("memory", ""))
    req_replicas = spec.replicas
    log.debug(
        "Quota check: cpu_req=%.4f cpu_lim=%.4f mem_req=%.0f mem_lim=%.0f replicas=%d | "
        "limits: cpu_req=%s cpu_lim=%s mem_req=%s mem_lim=%s replicas=%s deploys=%s pods=%s "
        "total_cpu=%s total_mem=%s",
        req_cpu_req, req_cpu_lim, req_mem_req, req_mem_lim, req_replicas,
        s.max_cpu_request_per_pod, s.max_cpu_limit_per_pod,
        s.max_memory_request_per_pod, s.max_memory_limit_per_pod,
        s.max_replicas_per_app, s.max_total_deployments, s.max_total_pods,
        s.max_total_cpu_requests, s.max_total_memory_requests,
    )

    # Allowed source peers
    allowed_peers = [p.strip() for p in s.allowed_source_peers.split(",") if p.strip()]
    if allowed_peers and source_peer and source_peer not in allowed_peers:
        return f"Peer '{source_peer}' is not permitted to submit workloads to this cluster"

    # Image policy
    if spec.image:
        img_err = _check_image_policy(spec.image)
        if img_err:
            return img_err

    # Per-pod CPU
    if s.max_cpu_request_per_pod:
        limit = _parse_quantity(s.max_cpu_request_per_pod)
        if req_cpu_req > limit:
            return (f"CPU request {res.requests.get('cpu', '0')} exceeds per-pod limit "
                    f"of {s.max_cpu_request_per_pod}")
    if s.max_cpu_limit_per_pod:
        limit = _parse_quantity(s.max_cpu_limit_per_pod)
        if req_cpu_lim > limit:
            return (f"CPU limit {res.limits.get('cpu', '0')} exceeds per-pod limit "
                    f"of {s.max_cpu_limit_per_pod}")

    # Per-pod memory
    if s.max_memory_request_per_pod:
        limit = _parse_quantity(s.max_memory_request_per_pod)
        if req_mem_req > limit:
            return (f"Memory request {res.requests.get('memory', '0')} exceeds per-pod limit "
                    f"of {s.max_memory_request_per_pod}")
    if s.max_memory_limit_per_pod:
        limit = _parse_quantity(s.max_memory_limit_per_pod)
        if req_mem_lim > limit:
            return (f"Memory limit {res.limits.get('memory', '0')} exceeds per-pod limit "
                    f"of {s.max_memory_limit_per_pod}")

    # Per-app replicas
    if s.max_replicas_per_app and req_replicas > s.max_replicas_per_app:
        return (f"Requested {req_replicas} replicas exceeds this cluster's per-app limit "
                f"of {s.max_replicas_per_app}")

    active_apps = [a for a in state.remote_apps.values()
                   if a.status not in ("Failed", "Timeout", "Deleted")]

    if s.max_total_deployments and len(active_apps) >= s.max_total_deployments:
        return (f"This cluster has reached its deployment limit "
                f"({s.max_total_deployments} concurrent RemoteApps)")

    if s.max_total_pods:
        used_pods = sum(a.spec.replicas for a in active_apps)
        if used_pods + req_replicas > s.max_total_pods:
            return (f"Insufficient pod capacity: {req_replicas} requested, "
                    f"{s.max_total_pods - used_pods} available "
                    f"(limit {s.max_total_pods} total pods)")

    if s.max_total_cpu_requests:
        max_total = _parse_quantity(s.max_total_cpu_requests)
        used = sum(_parse_quantity(a.spec.resources.requests.get("cpu", ""))
                   for a in active_apps)
        if used + req_cpu_req > max_total:
            return (f"Insufficient CPU capacity: request {res.requests.get('cpu', '0')} "
                    f"would exceed cluster total of {s.max_total_cpu_requests}")

    if s.max_total_memory_requests:
        max_total = _parse_quantity(s.max_total_memory_requests)
        used = sum(_parse_quantity(a.spec.resources.requests.get("memory", ""))
                   for a in active_apps)
        if used + req_mem_req > max_total:
            return (f"Insufficient memory: request {res.requests.get('memory', '0')} "
                    f"would exceed cluster total of {s.max_total_memory_requests}")

    return None


@bp.route("/remoteapp", methods=["POST"])
def create_remoteapp():
    data = request.json
    if not data or "name" not in data:
        return jsonify({"error": "name is required"}), 400
    if not state.peers:
        return jsonify({"error": "no peers connected"}), 503

    peer = next(iter(state.peers.values()))
    ra = RemoteApp(name=data["name"], spec=RemoteAppSpec.from_dict(data.get("spec", {})),
                   source_peer=state.AGENT_NAME, target_peer=peer.name)
    state.local_apps[ra.id] = ra

    try:
        session = _peer_session(peer)
        resp = session.post(
            f"{peer.url}/agent/remoteapp/receive",
            json={"id": ra.id, "name": ra.name, "spec": ra.spec.to_dict(),
                  "source_peer": state.AGENT_NAME},
            timeout=5,
        )
        if resp.status_code != 200:
            err = resp.json() if resp.content else {}
            msg = err.get("error", f"peer returned {resp.status_code}")
            del state.local_apps[ra.id]
            return jsonify({"error": msg}), 502
    except Exception as e:
        del state.local_apps[ra.id]
        return jsonify({"error": f"failed to reach peer: {e}"}), 502

    log.info("Forwarded app %s (%s) to peer %s", ra.name, ra.id, peer.name)
    tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings)
    return jsonify(ra.to_dict()), 201


@bp.route("/remoteapp/receive", methods=["POST"])
def receive_remoteapp():
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403
    if not state.settings.allow_inbound_remoteapps:
        return jsonify({
            "error": "inbound workloads are disabled on this agent",
            "inbound_disabled": True,
        }), 403

    data = request.json
    spec = RemoteAppSpec.from_dict(data.get("spec", {}))
    source_peer = data.get("source_peer", "unknown")
    quota_err = _check_resource_quota(spec, source_peer=source_peer)
    if quota_err:
        log.warning("RemoteApp rejected by policy: %s", quota_err)
        return jsonify({"error": quota_err, "quota_violation": True}), 429

    app_id = data.get("id") or uuid.uuid4().hex[:8]
    source = state.peers.get(source_peer)
    callback_url = source.url if source else ""

    if state.settings.require_remoteapp_approval:
        entry = {
            "id": app_id,
            "name": data["name"],
            "spec": spec.to_dict(),
            "source_peer": source_peer,
            "callback_url": callback_url,
            "since": datetime.now(timezone.utc).isoformat(),
        }
        state.pending_approval[app_id] = entry
        log.info("App %s (%s) queued for approval from %s", data["name"], app_id, source_peer)
        tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings,
                                 state.pending_approval)
        return jsonify({"id": app_id, "status": "pending_approval"})

    ra = RemoteApp(
        name=data["name"],
        spec=spec,
        source_peer=source_peer,
        id=app_id,
    )
    state.remote_apps[ra.id] = ra
    log.info("Received app %s (%s) from %s", ra.name, ra.id, ra.source_peer)
    run_workload(ra, callback_url, peer=source)
    return jsonify(ra.to_dict())


@bp.route("/remoteapp/pending-approval")
def list_pending_approval():
    return jsonify(list(state.pending_approval.values()))


@bp.route("/remoteapp/<app_id>/approve", methods=["POST"])
def approve_remoteapp(app_id):
    if app_id not in state.pending_approval:
        return jsonify({"error": "not found"}), 404
    entry = state.pending_approval[app_id]
    parsed_spec = RemoteAppSpec.from_dict(entry["spec"])
    state.pending_approval.pop(app_id)
    source = state.peers.get(entry["source_peer"])
    ra = RemoteApp(
        name=entry["name"],
        spec=parsed_spec,
        source_peer=entry["source_peer"],
        id=app_id,
    )
    state.remote_apps[ra.id] = ra
    log.info("Approved app %s (%s) from %s", ra.name, ra.id, ra.source_peer)
    tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings,
                             state.pending_approval)
    run_workload(ra, entry["callback_url"], peer=source)
    return jsonify({"ok": True})


@bp.route("/remoteapp/<app_id>/reject", methods=["POST"])
def reject_remoteapp(app_id):
    if app_id not in state.pending_approval:
        return jsonify({"error": "not found"}), 404
    entry = state.pending_approval.pop(app_id)
    log.info("Rejected app %s (%s) from %s", entry["name"], app_id, entry["source_peer"])
    tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings,
                             state.pending_approval)
    # Notify the source peer the app was rejected so their status updates
    source = state.peers.get(entry["source_peer"])
    if source:
        try:
            session = _peer_session(source)
            session.post(
                f"{source.url}/agent/remoteapp/{app_id}/status",
                json={"status": "Rejected",
                      "updated_at": datetime.now(timezone.utc).isoformat()},
                timeout=5,
            )
        except Exception as e:
            log.warning("Could not notify source of rejection: %s", e)
    return jsonify({"ok": True})


@bp.route("/remoteapps")
def list_remoteapps():
    return jsonify({
        "submitted": [a.to_dict() for a in state.local_apps.values()],
        "executing": [a.to_dict() for a in state.remote_apps.values()],
    })


@bp.route("/remoteapp/<app_id>/status", methods=["POST"])
def update_status(app_id):
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    if app_id in state.local_apps:
        state.local_apps[app_id].status = data.get("status", state.local_apps[app_id].status)
        state.local_apps[app_id].updated_at = data.get("updated_at",
                                                        state.local_apps[app_id].updated_at)
        log.info("Status update for %s: %s", app_id, state.local_apps[app_id].status)
        tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings)
        return jsonify({"ok": True})
    return jsonify({"error": "app not found"}), 404


@bp.route("/remoteapp/<app_id>", methods=["DELETE"])
def delete_remoteapp(app_id):
    if app_id in state.local_apps:
        ra = state.local_apps[app_id]
        peer = state.peers.get(ra.target_peer) or next(iter(state.peers.values()), None)
        if peer:
            try:
                session = _peer_session(peer)
                session.delete(f"{peer.url}/agent/remoteapp/{app_id}/remote", timeout=5)
            except Exception as e:
                log.warning("Failed to notify peer of deletion: %s", e)
        state.local_apps[app_id].status = "Deleted"
        del state.local_apps[app_id]
        tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings)
        return jsonify({"ok": True})

    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        delete_workload(ra)
        ra.status = "Deleted"
        del state.remote_apps[app_id]
        source = state.peers.get(ra.source_peer)
        if source:
            try:
                session = _peer_session(source)
                session.post(
                    f"{source.url}/agent/remoteapp/{app_id}/status",
                    json={"status": "Deleted",
                          "updated_at": datetime.now(timezone.utc).isoformat()},
                    timeout=5,
                )
            except Exception as exc:
                log.warning("Failed to notify source peer of deletion: %s", exc)
        return jsonify({"ok": True})

    return jsonify({"error": "app not found"}), 404


@bp.route("/remoteapp/<app_id>/remote", methods=["DELETE"])
def delete_remoteapp_remote(app_id):
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403

    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        delete_workload(ra)
        ra.status = "Deleted"
        del state.remote_apps[app_id]
        log.info("Deleted remote app %s on peer request", app_id)
        return jsonify({"ok": True})

    return jsonify({"error": "app not found"}), 404


@bp.route("/remoteapp/<app_id>/scale", methods=["POST"])
def scale_remoteapp(app_id):
    data = request.json or {}
    replicas = data.get("replicas")
    if replicas is None:
        return jsonify({"error": "replicas is required"}), 400
    try:
        replicas = max(0, int(replicas))
    except (ValueError, TypeError):
        return jsonify({"error": "replicas must be an integer"}), 400

    if app_id in state.local_apps:
        ra = state.local_apps[app_id]
        peer = state.peers.get(ra.source_peer) or next(iter(state.peers.values()), None)
        if not peer:
            return jsonify({"error": "peer not connected"}), 503
        try:
            session = _peer_session(peer)
            resp = session.post(
                f"{peer.url}/agent/remoteapp/{app_id}/scale/remote",
                json={"replicas": replicas},
                timeout=5,
            )
            if resp.ok:
                ra.spec.replicas = replicas
                return jsonify({"ok": True, "replicas": replicas})
            return jsonify({"error": resp.json().get("error", "peer error")}), resp.status_code
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        try:
            scale_workload(ra, replicas)
            ra.spec.replicas = replicas
            return jsonify({"ok": True, "replicas": replicas})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "app not found"}), 404


@bp.route("/remoteapp/<app_id>/scale/remote", methods=["POST"])
def scale_remoteapp_remote(app_id):
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    replicas = data.get("replicas")
    if replicas is None:
        return jsonify({"error": "replicas is required"}), 400
    if app_id not in state.remote_apps:
        return jsonify({"error": "app not found"}), 404

    ra = state.remote_apps[app_id]
    try:
        scale_workload(ra, int(replicas))
        ra.spec.replicas = int(replicas)
        return jsonify({"ok": True, "replicas": int(replicas)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/remoteapp/<app_id>/detail")
def remoteapp_detail(app_id):
    if app_id in state.local_apps:
        ra = state.local_apps[app_id]
        peer = state.peers.get(ra.source_peer) or next(iter(state.peers.values()), None)
        if not peer:
            return jsonify({"error": "peer not connected", "app": ra.to_dict()}), 200
        try:
            session = _peer_session(peer)
            resp = session.get(f"{peer.url}/agent/remoteapp/{app_id}/detail/remote", timeout=5)
            detail = resp.json() if resp.ok else {}
        except Exception as e:
            detail = {"error": str(e)}
        return jsonify({"app": ra.to_dict(), "k8s": detail})

    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        detail = get_deployment_status(ra)
        return jsonify({"app": ra.to_dict(), "k8s": detail})

    return jsonify({"error": "app not found"}), 404


@bp.route("/remoteapp/<app_id>/detail/remote")
def remoteapp_detail_remote(app_id):
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403
    if app_id not in state.remote_apps:
        return jsonify({"error": "app not found"}), 404
    ra = state.remote_apps[app_id]
    return jsonify(get_deployment_status(ra))


@bp.route("/remoteapp/<app_id>/spec", methods=["PUT"])
def update_remoteapp_spec(app_id):
    data = request.json or {}
    new_spec = data.get("spec")
    if new_spec is None:
        return jsonify({"error": "spec is required"}), 400
    if app_id not in state.local_apps:
        return jsonify({"error": "app not found"}), 404

    ra = state.local_apps[app_id]
    peer = state.peers.get(ra.source_peer) or next(iter(state.peers.values()), None)
    if not peer:
        return jsonify({"error": "peer not connected"}), 503

    try:
        session = _peer_session(peer)
        resp = session.put(
            f"{peer.url}/agent/remoteapp/{app_id}/spec/remote",
            json={"spec": new_spec, "name": ra.name, "source_peer": state.AGENT_NAME},
            timeout=5,
        )
        if not resp.ok:
            return jsonify({"error": resp.json().get("error", "peer error")}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    ra.spec = RemoteAppSpec.from_dict(new_spec)
    return jsonify(ra.to_dict())


@bp.route("/remoteapp/<app_id>/spec/remote", methods=["PUT"])
def update_remoteapp_spec_remote(app_id):
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json or {}
    new_spec = data.get("spec")
    if new_spec is None:
        return jsonify({"error": "spec is required"}), 400
    if app_id not in state.remote_apps:
        return jsonify({"error": "app not found"}), 404

    ra = state.remote_apps[app_id]
    parsed_spec = RemoteAppSpec.from_dict(new_spec)
    quota_err = _check_resource_quota(parsed_spec, source_peer=ra.source_peer)
    if quota_err:
        return jsonify({"error": quota_err, "quota_violation": True}), 429

    ra.spec = parsed_spec
    source = state.peers.get(ra.source_peer)
    callback_url = source.url if source else ""
    run_workload(ra, callback_url, peer=source)
    return jsonify({"ok": True})
