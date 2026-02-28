import base64
import logging

from flask import Blueprint, request, jsonify, Response

from porpulsion import state
from porpulsion.peering import verify_peer, identify_peer
from porpulsion.channel import get_channel

log = logging.getLogger("porpulsion.routes.tunnels")

bp = Blueprint("tunnels", __name__)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# Hop-by-hop headers that must not be forwarded
_HOP_BY_HOP = {"host", "transfer-encoding", "connection", "keep-alive",
               "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "upgrade", "content-encoding"}


# ── User-facing proxy (submitting side) ───────────────────────
#
# Any request to /remoteapp/<id>/proxy/<port>[/<path>] is forwarded over
# mTLS to the executing peer at /remoteapp/<id>/proxy-remote/<port>[/<path>],
# which resolves the pod and makes the real HTTP call.

@bp.route("/remoteapp/<app_id>/proxy/<int:port>",
          defaults={"subpath": ""},
          methods=_PROXY_METHODS)
@bp.route("/remoteapp/<app_id>/proxy/<int:port>/<path:subpath>",
          methods=_PROXY_METHODS)
def proxy_remoteapp(app_id, port, subpath):
    """User-facing: proxy HTTP through to a pod on the peer cluster."""
    if app_id not in state.local_apps:
        return jsonify({"error": "app not found"}), 404

    ra = state.local_apps[app_id]
    peer = state.peers.get(ra.target_peer) or next(iter(state.peers.values()), None)
    if not peer:
        return jsonify({"error": "peer not connected"}), 503

    qs = request.query_string.decode()
    path = (subpath + ("?" + qs if qs else "")) if subpath else ("?" + qs if qs else "")
    fwd_headers = {k: v for k, v in request.headers if k.lower() not in _HOP_BY_HOP}

    try:
        ch = get_channel(peer.name)
        result = ch.call("proxy/request", {
            "app_id": app_id,
            "port": port,
            "method": request.method,
            "path": path,
            "headers": fwd_headers,
            "body": base64.b64encode(request.get_data()).decode(),
        }, timeout=30)
    except Exception as exc:
        return jsonify({"error": f"failed to reach peer: {exc}"}), 502

    resp_headers = {k: v for k, v in result.get("headers", {}).items()
                    if k.lower() not in _HOP_BY_HOP}
    body = base64.b64decode(result.get("body", ""))
    return Response(body, status=result.get("status", 502), headers=resp_headers)


# ── Peer-facing proxy (executing side) ────────────────────────

@bp.route("/remoteapp/<app_id>/proxy-remote/<int:port>",
          defaults={"subpath": ""},
          methods=_PROXY_METHODS)
@bp.route("/remoteapp/<app_id>/proxy-remote/<int:port>/<path:subpath>",
          methods=_PROXY_METHODS)
def proxy_remoteapp_remote(app_id, port, subpath):
    """Peer-facing: resolve pod and proxy the request to it."""
    if not verify_peer(request, state.peers):
        return jsonify({"error": "unauthorized"}), 403

    if not state.settings.allow_inbound_tunnels:
        return jsonify({"error": "inbound tunnels are disabled on this agent"}), 403

    # Empty allowlist = deny all; non-empty = check peer (or peer/appid) entries
    raw_tokens = [p.strip() for p in state.settings.allowed_tunnel_peers.split(",") if p.strip()]
    peer_name = identify_peer(request, state.peers)
    if not raw_tokens:
        return jsonify({"error": "no tunnel peers are permitted on this agent"}), 403

    # Partition tokens into bare peer names vs per-app entries for this peer
    peer_all = set()    # bare "peername" tokens — all apps allowed for this peer
    peer_apps = set()   # "peername/appid" tokens — specific apps allowed for this peer
    for token in raw_tokens:
        parts = token.split("/", 1)
        if len(parts) == 1:
            peer_all.add(parts[0])
        else:
            if parts[0] == peer_name:
                peer_apps.add(parts[1])

    if peer_name not in peer_all and not peer_apps:
        return jsonify({"error": f"peer '{peer_name}' is not on the tunnel allowlist"}), 403

    # If peer has per-app restrictions, check the requested app_id
    if peer_name not in peer_all and app_id not in peer_apps:
        return jsonify({"error": f"app '{app_id}' is not permitted for peer '{peer_name}'"}), 403

    if app_id not in state.remote_apps:
        return jsonify({"error": "app not found"}), 404

    from porpulsion.k8s.tunnel import proxy_request
    qs = request.query_string.decode()
    path_with_qs = subpath + ("?" + qs if qs else "")

    try:
        status, resp_headers, body = proxy_request(
            remote_app_id=app_id,
            port=port,
            method=request.method,
            path=path_with_qs,
            headers=dict(request.headers),
            body=request.get_data(),
        )
        return Response(body, status=status, headers=resp_headers)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": f"proxy error: {exc}"}), 502
