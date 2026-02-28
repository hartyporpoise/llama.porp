import logging
import secrets
import uuid
from datetime import datetime, timezone

import requests as _req
import urllib3 as _urllib3
from flask import Blueprint, request, jsonify

from porpulsion import state, tls
from porpulsion.models import Peer
from porpulsion.peering import initiate_peering

log = logging.getLogger("porpulsion.routes.peers")

bp = Blueprint("peers", __name__)


def _peer_session(peer: Peer | None = None) -> _req.Session:
    session = _req.Session()
    session.cert = (state.AGENT_CERT_PATH, state.AGENT_KEY_PATH)
    session.verify = tls.peer_ca_path(peer.name) if (peer and peer.ca_pem) else False
    return session


def _rebuild_mtls_server():
    """Trigger an mTLS server rebuild via the callback set by agent.py."""
    if state._rebuild_mtls_callback:
        state._rebuild_mtls_callback()


@bp.route("/status")
def status():
    return jsonify({
        "agent": state.AGENT_NAME,
        "peers": [p.to_dict() for p in state.peers.values()],
        "local_apps": len(state.local_apps),
        "remote_apps": len(state.remote_apps),
    })


@bp.route("/peers")
def list_peers():
    result = [p.to_dict() for p in state.peers.values()]
    for url, info in state.pending_peers.items():
        entry = {
            "name": info.get("name", url),
            "url": url,
            "connected_at": info["since"],
            "status": info.get("status", "connecting"),
            "attempts": info["attempts"],
        }
        if "error" in info:
            entry["error"] = info["error"]
        result.append(entry)
    return jsonify(result)


@bp.route("/peer", methods=["POST"])
def accept_peer():
    """
    Peering endpoint — two steps:

    Step 1 (invite): initiator sends our invite token + their cert.
    Step 2 (confirm): called by accept_inbound() when operator clicks Accept.
    """
    data = request.json or {}
    peer_name = data.get("name", "unknown")
    peer_url  = data.get("url", "")
    peer_ca   = data.get("ca", "")

    presented_token = request.headers.get("X-Invite-Token", "")

    # ── Confirmation path (no token, ca in body) ─────────────
    if peer_ca and not presented_token:
        presented_fp = tls.cert_fingerprint(peer_ca)
        log.info("Confirmation from %s (url=%r), presented_fp=%s, pending keys=%s",
                 peer_name, peer_url, presented_fp[:16], list(state.pending_peers.keys()))
        awaiting = state.pending_peers.get(peer_url)
        if not awaiting:
            for _url, _info in state.pending_peers.items():
                if _info.get("status") == "awaiting_confirmation":
                    stored = _info.get("ca_pem", "")
                    stored_fp = tls.cert_fingerprint(stored) if stored else "(empty)"
                    if stored and stored_fp == presented_fp:
                        awaiting = _info
                        peer_url = _url
                        break
        if awaiting and awaiting.get("status") == "awaiting_confirmation":
            tls.write_temp_pem(peer_ca.encode(), f"peer-ca-{peer_name}")
            state.peers[peer_name] = Peer(name=peer_name, url=peer_url, ca_pem=peer_ca)
            state.pending_peers.pop(peer_url, None)
            _rebuild_mtls_server()
            tls.save_peers(state.NAMESPACE, state.peers)
            log.info("Peering confirmed by %s — fully connected", peer_name)
            return jsonify({"name": state.AGENT_NAME, "status": "peered",
                            "ca": state.AGENT_CA_PEM.decode()})
        log.warning("accept_peer: unexpected ca-only request from %s (no matching pending)", peer_name)
        return jsonify({"error": "no pending outbound connection for this peer"}), 403

    # ── Invite path ───────────────────────────────────────────
    if not presented_token or not secrets.compare_digest(presented_token, state.invite_token):
        log.warning("accept_peer: bad or missing invite token from %s", request.remote_addr)
        return jsonify({"error": "invalid token"}), 403

    state.invite_token = secrets.token_hex(32)
    tls.persist_token(state.NAMESPACE, state.invite_token)
    log.info("Invite token consumed — queuing inbound request from %s", peer_name)

    req_id = uuid.uuid4().hex[:12]
    state.pending_inbound[req_id] = {
        "id": req_id,
        "name": peer_name,
        "url": peer_url,
        "ca_pem": peer_ca,
        "since": datetime.now(timezone.utc).isoformat(),
    }
    if peer_ca:
        tls.write_temp_pem(peer_ca.encode(), f"peer-ca-{peer_name}")

    return jsonify({"name": state.AGENT_NAME, "status": "pending",
                    "ca": state.AGENT_CA_PEM.decode()})


@bp.route("/peers/inbound", methods=["GET"])
def list_inbound():
    _hide = {"ca_pem"}
    return jsonify([{"id": req_id, **{k: v for k, v in r.items() if k not in _hide}}
                    for req_id, r in state.pending_inbound.items()])


@bp.route("/peers/inbound/<req_id>/accept", methods=["POST"])
def accept_inbound(req_id):
    if req_id not in state.pending_inbound:
        return jsonify({"error": "request not found"}), 404

    info = state.pending_inbound.pop(req_id)
    peer_name = info["name"]
    peer_url  = info["url"]
    peer_ca   = info.get("ca_pem", "")

    _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
    session = _req.Session()
    session.cert = None
    session.verify = False

    try:
        resp = session.post(
            f"{peer_url}/peer",
            json={"name": state.AGENT_NAME, "url": state.SELF_URL,
                  "ca": state.AGENT_CA_PEM.decode()},
            timeout=5,
        )
        if resp.status_code == 200:
            resp_data = resp.json()
            their_ca = resp_data.get("ca", peer_ca)
            tls.write_temp_pem(their_ca.encode() if isinstance(their_ca, str) else their_ca,
                               f"peer-ca-{peer_name}")
            state.peers[peer_name] = Peer(name=peer_name, url=peer_url, ca_pem=their_ca)
            _rebuild_mtls_server()
            tls.save_peers(state.NAMESPACE, state.peers)
            log.info("Accepted and confirmed peering with %s", peer_name)
            return jsonify({"ok": True, "peer": peer_name})
        log.warning("accept_inbound: initiator returned %s: %s", resp.status_code, resp.text[:200])
        state.pending_inbound[req_id] = info
        return jsonify({"error": f"initiator returned {resp.status_code}"}), 502
    except Exception as exc:
        log.warning("accept_inbound: could not reach %s: %s", peer_url, exc)
        state.pending_inbound[req_id] = info
        return jsonify({"error": str(exc)}), 502


@bp.route("/peers/inbound/<req_id>", methods=["DELETE"])
def reject_inbound(req_id):
    if req_id not in state.pending_inbound:
        return jsonify({"error": "request not found"}), 404
    info = state.pending_inbound.pop(req_id)
    log.info("Rejected inbound peering request from %s", info["name"])
    return jsonify({"ok": True})


@bp.route("/peers/<peer_name>", methods=["DELETE"])
def remove_peer(peer_name):
    if peer_name not in state.peers:
        return jsonify({"error": "peer not found"}), 404

    peer = state.peers.pop(peer_name)
    log.info("Removed peer %s", peer_name)

    try:
        session = _peer_session()
        session.verify = False
        _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
        session.post(
            f"{peer.url}/peer/disconnect",
            json={"name": state.AGENT_NAME},
            timeout=3,
        )
    except Exception as exc:
        log.debug("Could not notify %s of disconnection: %s", peer_name, exc)

    for ra in list(state.local_apps.values()):
        if ra.target_peer == peer_name:
            ra.status = "Failed"
            log.info("Marked app %s as Failed (peer %s removed)", ra.id, peer_name)

    _rebuild_mtls_server()
    tls.save_peers(state.NAMESPACE, state.peers)
    return jsonify({"ok": True, "removed": peer_name})


@bp.route("/peer/disconnect", methods=["POST"])
def peer_disconnect():
    data = request.json or {}
    peer_name = data.get("name", "")
    removed = False
    if peer_name and peer_name in state.peers:
        state.peers.pop(peer_name)
        removed = True
        log.info("Peer %s disconnected us — removed from peer list", peer_name)
        for ra in list(state.local_apps.values()):
            if ra.target_peer == peer_name:
                ra.status = "Failed"
                log.info("Marked app %s as Failed (peer %s disconnected)", ra.id, peer_name)
        _rebuild_mtls_server()
        tls.save_peers(state.NAMESPACE, state.peers)
    return jsonify({"ok": True, "removed": removed})


@bp.route("/peers/retry", methods=["POST"])
def retry_connecting_peer():
    data = request.json or {}
    peer_url       = data.get("url", "")
    token          = data.get("invite_token", "")
    ca_fingerprint = data.get("ca_fingerprint", "")
    if not peer_url:
        return jsonify({"error": "url is required"}), 400
    if not token:
        return jsonify({"error": "invite_token is required to retry"}), 400
    if not ca_fingerprint:
        return jsonify({"error": "ca_fingerprint is required to retry"}), 400

    state.pending_peers[peer_url] = {
        "name": peer_url, "url": peer_url,
        "since": datetime.now(timezone.utc).isoformat(), "attempts": 0,
    }
    initiate_peering(state.AGENT_NAME, state.SELF_URL, peer_url, token,
                     state.peers, state.pending_peers,
                     ca_pem_str=state.AGENT_CA_PEM.decode(), expected_ca_fp=ca_fingerprint)
    log.info("Retrying peering with %s", peer_url)
    return jsonify({"ok": True, "message": f"Retrying connection to {peer_url}"})


@bp.route("/peers/connecting", methods=["DELETE"])
def cancel_connecting_peer():
    peer_url = request.args.get("url", "")
    if not peer_url:
        return jsonify({"error": "url query parameter required"}), 400
    if peer_url in state.pending_peers:
        del state.pending_peers[peer_url]
        log.info("Cancelled pending connection to %s", peer_url)
        return jsonify({"ok": True, "cancelled": peer_url})
    return jsonify({"error": "no pending connection to that URL"}), 404


@bp.route("/peers/connect", methods=["POST"])
def connect_peer():
    data = request.json or {}
    url            = data.get("url", "").rstrip("/")
    token          = data.get("invite_token", "")
    ca_fingerprint = data.get("ca_fingerprint", "")
    if not url:
        return jsonify({"error": "url is required"}), 400
    if not token:
        return jsonify({"error": "invite_token is required"}), 400
    if not ca_fingerprint:
        return jsonify({"error": "ca_fingerprint is required"}), 400

    state.pending_peers[url] = {
        "name": url, "url": url,
        "since": datetime.now(timezone.utc).isoformat(), "attempts": 0,
    }
    initiate_peering(state.AGENT_NAME, state.SELF_URL, url, token,
                     state.peers, state.pending_peers,
                     ca_pem_str=state.AGENT_CA_PEM.decode(), expected_ca_fp=ca_fingerprint)
    return jsonify({"ok": True, "message": f"Peering initiated with {url}"})


@bp.route("/token")
def get_token():
    fp = tls.cert_fingerprint(state.AGENT_CA_PEM)
    return jsonify({
        "agent": state.AGENT_NAME,
        "invite_token": state.invite_token,
        "self_url": state.SELF_URL,
        "cert_fingerprint": fp,
        "ca_pem": state.AGENT_CA_PEM.decode(),
    })
