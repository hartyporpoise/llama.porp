"""
Message handlers for incoming WebSocket frames.

Each function is registered on a PeerChannel by channel._register_handlers().
Handlers for request-type messages return a dict payload (sent as the reply).
Handlers for push-type messages return None.

All inbound peer authentication has already been done by the WS endpoint
before the socket is handed to the channel — these handlers trust the caller.
"""
import base64
import logging
from datetime import datetime, timezone

log = logging.getLogger("porpulsion.channel_handlers")


# ── RemoteApp ─────────────────────────────────────────────────

def handle_remoteapp_receive(payload: dict) -> dict:
    """Accept a RemoteApp submission from a peer."""
    from porpulsion import state, tls
    from porpulsion.models import RemoteApp, RemoteAppSpec
    from porpulsion.routes.workloads import _check_resource_quota

    from porpulsion.notifications import add_notification

    if not state.settings.allow_inbound_remoteapps:
        raise RuntimeError("inbound workloads are disabled on this agent")

    spec = RemoteAppSpec.from_dict(payload.get("spec", {}))
    source_peer = payload.get("source_peer", "unknown")
    quota_err = _check_resource_quota(spec, source_peer=source_peer)
    if quota_err:
        add_notification(
            level="error",
            title=f"Workload rejected from {source_peer}",
            message=f"{payload.get('name', '?')!r}: {quota_err}",
        )
        raise RuntimeError(quota_err)

    app_id = payload.get("id") or __import__("uuid").uuid4().hex[:8]
    source = state.peers.get(source_peer)

    if state.settings.require_remoteapp_approval:
        entry = {
            "id": app_id,
            "name": payload["name"],
            "spec": spec.to_dict(),
            "source_peer": source_peer,
            "callback_url": source_peer,   # channel key — not a URL anymore
            "since": datetime.now(timezone.utc).isoformat(),
        }
        state.pending_approval[app_id] = entry
        log.info("App %s queued for approval (via channel) from %s", app_id, source_peer)
        tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings,
                                 state.pending_approval)
        add_notification(
            level="info",
            title="Approval required",
            message=f"{payload['name']!r} from {source_peer} is waiting for your approval.",
        )
        return {"id": app_id, "status": "pending_approval"}

    ra = RemoteApp(
        name=payload["name"], spec=spec,
        source_peer=source_peer, id=app_id,
    )
    state.remote_apps[ra.id] = ra
    log.info("Received app %s (%s) via channel from %s", ra.name, ra.id, source_peer)

    from porpulsion.k8s.executor import run_workload
    # Pass the peer name as callback_url — executor will route via channel
    run_workload(ra, source_peer, peer=source)
    return ra.to_dict()


def handle_remoteapp_status(payload: dict):
    """Status update pushed from executor back to the submitting peer."""
    from porpulsion import state, tls
    from porpulsion.notifications import add_notification
    app_id = payload.get("id") or payload.get("app_id", "")
    status = payload.get("status", "")
    updated_at = payload.get("updated_at", datetime.now(timezone.utc).isoformat())

    if app_id in state.local_apps:
        ra = state.local_apps[app_id]
        ra.status = status
        ra.updated_at = updated_at
        log.info("Status update for %s: %s (via channel)", app_id, status)
        tls.save_state_configmap(state.NAMESPACE, state.local_apps, state.settings)
        if status.startswith("Failed") or status == "Timeout":
            add_notification(
                level="error",
                title=f"Workload failed: {ra.name}",
                message=f"{ra.name!r} on {ra.target_peer} → {status}.",
            )


def handle_remoteapp_delete(payload: dict) -> dict:
    """Delete a RemoteApp on this (executing) side."""
    from porpulsion import state
    from porpulsion.k8s.executor import delete_workload
    app_id = payload.get("id", "")
    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        delete_workload(ra)
        ra.status = "Deleted"
        del state.remote_apps[app_id]
        log.info("Deleted remote app %s (via channel)", app_id)
        return {"ok": True}
    raise RuntimeError("app not found")


def handle_remoteapp_scale(payload: dict) -> dict:
    """Scale a RemoteApp on this (executing) side."""
    from porpulsion import state
    from porpulsion.k8s.executor import scale_workload
    app_id   = payload.get("id", "")
    replicas = payload.get("replicas")
    if app_id not in state.remote_apps:
        raise RuntimeError("app not found")
    scale_workload(state.remote_apps[app_id], int(replicas))
    state.remote_apps[app_id].spec.replicas = int(replicas)
    return {"ok": True, "replicas": int(replicas)}


def handle_remoteapp_detail(payload: dict) -> dict:
    """Return k8s deployment detail for a RemoteApp."""
    from porpulsion import state
    from porpulsion.k8s.executor import get_deployment_status
    app_id = payload.get("id", "")
    if app_id not in state.remote_apps:
        raise RuntimeError("app not found")
    ra = state.remote_apps[app_id]
    result = get_deployment_status(ra)
    result["spec"] = ra.spec.to_dict()
    return result


def handle_remoteapp_logs(payload: dict) -> dict:
    """Return pod logs for a RemoteApp (executing on this cluster)."""
    from porpulsion import state
    from porpulsion.k8s.executor import get_pod_logs
    app_id = payload.get("id", "")
    if app_id not in state.remote_apps:
        raise RuntimeError("app not found")
    tail = int(payload.get("tail") or 200)
    pod_name = (payload.get("pod") or "").strip() or None
    order_by_time = payload.get("order") == "time"
    return get_pod_logs(state.remote_apps[app_id], tail=tail, pod_name=pod_name, order_by_time=order_by_time)


def handle_remoteapp_spec_update(payload: dict) -> dict:
    """Apply a new spec to a RemoteApp on the executing side."""
    from porpulsion import state
    from porpulsion.models import RemoteAppSpec
    from porpulsion.routes.workloads import _check_resource_quota
    from porpulsion.k8s.executor import run_workload
    app_id   = payload.get("id", "")
    new_spec = payload.get("spec")
    if app_id not in state.remote_apps:
        raise RuntimeError("app not found")
    ra = state.remote_apps[app_id]
    parsed = RemoteAppSpec.from_dict(new_spec)
    quota_err = _check_resource_quota(parsed, source_peer=ra.source_peer)
    if quota_err:
        raise RuntimeError(quota_err)
    ra.spec = parsed
    source = state.peers.get(ra.source_peer)
    run_workload(ra, ra.source_peer, peer=source)
    return {"ok": True}


# ── Proxy tunnel ──────────────────────────────────────────────

def handle_proxy_request(payload: dict, peer_name: str = "") -> dict:
    """
    Proxy an HTTP request to a local pod and return the response.
    Body is base64-encoded in the payload.
    """
    from porpulsion import state
    from porpulsion.k8s.tunnel import proxy_request

    app_id  = payload.get("app_id", "")
    port    = int(payload.get("port", 80))
    method  = payload.get("method", "GET")
    path    = payload.get("path", "")
    headers = payload.get("headers", {})
    body    = base64.b64decode(payload.get("body", ""))

    if not state.settings.allow_inbound_tunnels:
        raise RuntimeError("inbound tunnels are disabled on this agent")

    # Enforce per-peer tunnel allowlist. Empty string = allow all.
    allowed_raw = (state.settings.allowed_tunnel_peers or "").strip()
    if allowed_raw:
        allowed_tokens = {t.strip() for t in allowed_raw.split(",") if t.strip()}
        # Tokens are either "peer" (allow all apps from that peer) or "peer/app_id"
        if peer_name not in allowed_tokens and f"{peer_name}/{app_id}" not in allowed_tokens:
            raise RuntimeError(f"tunnel from peer '{peer_name}' is not permitted")

    if app_id not in state.remote_apps:
        raise RuntimeError("app not found")

    status, resp_headers, resp_body = proxy_request(
        remote_app_id=app_id, port=port,
        method=method, path=path,
        headers=headers, body=body,
    )
    return {
        "status": status,
        "headers": dict(resp_headers),
        "body": base64.b64encode(resp_body).decode(),
    }


# ── Peer lifecycle ────────────────────────────────────────────

def handle_peer_disconnect(payload: dict):
    """Peer is telling us it's disconnecting cleanly."""
    from porpulsion import state, tls
    from porpulsion.notifications import add_notification
    peer_name = payload.get("name", "")
    if peer_name and peer_name in state.peers:
        state.peers.pop(peer_name)
        state.peer_channels.pop(peer_name, None)
        affected = []
        for ra in list(state.local_apps.values()):
            if ra.target_peer == peer_name:
                ra.status = "Failed"
                affected.append(ra.name)
        tls.save_peers(state.NAMESPACE, state.peers)
        log.info("Peer %s disconnected (via channel)", peer_name)
        msg = f"Peer {peer_name!r} disconnected."
        if affected:
            msg += f" {len(affected)} workload(s) marked Failed: {', '.join(affected[:3])}{'…' if len(affected) > 3 else ''}."
        add_notification(level="warn", title=f"Peer disconnected: {peer_name}", message=msg)
