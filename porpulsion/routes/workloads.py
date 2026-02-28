import logging
from datetime import datetime, timezone

import requests as _req
from flask import Blueprint, request, jsonify

from porpulsion import state, tls
from porpulsion.models import RemoteApp
from porpulsion.peering import verify_peer
from porpulsion.k8s.executor import (
    run_workload, delete_workload, scale_workload, get_deployment_status,
)

log = logging.getLogger("porpulsion.routes.workloads")

bp = Blueprint("workloads", __name__)


def _peer_session(peer=None) -> _req.Session:
    session = _req.Session()
    session.cert = (state.AGENT_CERT_PATH, state.AGENT_KEY_PATH)
    session.verify = tls.peer_ca_path(peer.name) if (peer and peer.ca_pem) else False
    return session


def _check_resource_quota(spec: dict) -> str | None:
    s = state.settings
    req_cpu      = float(spec.get("cpu", 0) or 0)
    req_mem_mb   = int(spec.get("memory_mb", 0) or 0)
    req_replicas = int(spec.get("replicas", 1) or 1)

    if s.max_cpu_per_pod and req_cpu > s.max_cpu_per_pod:
        return (f"Requested {req_cpu} CPU cores exceeds this cluster's per-pod limit "
                f"of {s.max_cpu_per_pod} cores")
    if s.max_memory_mb_per_pod and req_mem_mb > s.max_memory_mb_per_pod:
        return (f"Requested {req_mem_mb} MiB exceeds this cluster's per-pod memory limit "
                f"of {s.max_memory_mb_per_pod} MiB")
    if s.max_replicas_per_app and req_replicas > s.max_replicas_per_app:
        return (f"Requested {req_replicas} replicas exceeds this cluster's per-app limit "
                f"of {s.max_replicas_per_app}")

    active_apps = [a for a in state.remote_apps.values()
                   if a.status not in ("Failed", "Timeout", "Deleted")]

    if s.max_total_deployments and len(active_apps) >= s.max_total_deployments:
        return (f"This cluster has reached its deployment limit "
                f"({s.max_total_deployments} concurrent RemoteApps)")
    if s.max_total_cpu:
        used_cpu = sum(float(a.spec.get("cpu", 0) or 0) for a in active_apps)
        if used_cpu + req_cpu > s.max_total_cpu:
            return (f"Insufficient CPU capacity: {req_cpu} requested, "
                    f"{s.max_total_cpu - used_cpu:.2f} available "
                    f"(limit {s.max_total_cpu} total)")
    if s.max_total_memory_mb:
        used_mem = sum(int(a.spec.get("memory_mb", 0) or 0) for a in active_apps)
        if used_mem + req_mem_mb > s.max_total_memory_mb:
            return (f"Insufficient memory: {req_mem_mb} MiB requested, "
                    f"{s.max_total_memory_mb - used_mem} MiB available "
                    f"(limit {s.max_total_memory_mb} MiB total)")
    return None


@bp.route("/remoteapp", methods=["POST"])
def create_remoteapp():
    data = request.json
    if not data or "name" not in data:
        return jsonify({"error": "name is required"}), 400
    if not state.peers:
        return jsonify({"error": "no peers connected"}), 503

    peer = next(iter(state.peers.values()))
    ra = RemoteApp(name=data["name"], spec=data.get("spec", {}),
                   source_peer=state.AGENT_NAME, target_peer=peer.name)
    state.local_apps[ra.id] = ra

    try:
        session = _peer_session(peer)
        resp = session.post(
            f"{peer.url}/remoteapp/receive",
            json={"id": ra.id, "name": ra.name, "spec": ra.spec,
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
    spec = data.get("spec", {})
    quota_err = _check_resource_quota(spec)
    if quota_err:
        log.warning("RemoteApp rejected by quota: %s", quota_err)
        return jsonify({"error": quota_err, "quota_violation": True}), 429

    ra = RemoteApp(
        name=data["name"],
        spec=spec,
        source_peer=data.get("source_peer", "unknown"),
        id=data.get("id"),
    )
    state.remote_apps[ra.id] = ra
    log.info("Received app %s (%s) from %s", ra.name, ra.id, ra.source_peer)

    source = state.peers.get(ra.source_peer)
    callback_url = source.url if source else ""
    run_workload(ra, callback_url, peer=source)
    return jsonify(ra.to_dict())


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
                session.delete(f"{peer.url}/remoteapp/{app_id}/remote", timeout=5)
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
                    f"{source.url}/remoteapp/{app_id}/status",
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
                f"{peer.url}/remoteapp/{app_id}/scale/remote",
                json={"replicas": replicas},
                timeout=5,
            )
            if resp.ok:
                ra.spec["replicas"] = replicas
                return jsonify({"ok": True, "replicas": replicas})
            return jsonify({"error": resp.json().get("error", "peer error")}), resp.status_code
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    if app_id in state.remote_apps:
        ra = state.remote_apps[app_id]
        try:
            scale_workload(ra, replicas)
            ra.spec["replicas"] = replicas
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
        ra.spec["replicas"] = int(replicas)
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
            resp = session.get(f"{peer.url}/remoteapp/{app_id}/detail/remote", timeout=5)
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
            f"{peer.url}/remoteapp/{app_id}/spec/remote",
            json={"spec": new_spec, "name": ra.name, "source_peer": state.AGENT_NAME},
            timeout=5,
        )
        if not resp.ok:
            return jsonify({"error": resp.json().get("error", "peer error")}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    ra.spec = new_spec
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
    quota_err = _check_resource_quota(new_spec)
    if quota_err:
        return jsonify({"error": quota_err, "quota_violation": True}), 429

    ra.spec = new_spec
    source = state.peers.get(ra.source_peer)
    callback_url = source.url if source else ""
    run_workload(ra, callback_url, peer=source)
    return jsonify({"ok": True})
