"""
WebSocket endpoint for peer-to-peer channels.

Peers connect to /agent/ws after the initial peering handshake. Authentication
uses the X-Agent-Ca header (the connecting peer's CA PEM) rather than a TLS
client cert, because the WS upgrade goes through nginx on port 8000 where no
client cert is available. We verify the CA fingerprint against known peers.
"""
import base64
import logging

from flask import Blueprint, request
from flask_sock import Sock

from porpulsion import state
from porpulsion.tls import cert_fingerprint

log = logging.getLogger("porpulsion.routes.ws")

bp   = Blueprint("ws", __name__)
sock = Sock()   # bound to the Flask app in agent.py


def _identify_peer_by_ca(ca_pem: str) -> str | None:
    """Return the peer name whose stored CA fingerprint matches ca_pem, or None."""
    if not ca_pem:
        return None
    try:
        incoming_fp = cert_fingerprint(ca_pem)
    except Exception as e:
        log.warning("WS auth: could not fingerprint incoming CA: %s", e)
        return None
    for peer in state.peers.values():
        if not peer.ca_pem:
            log.debug("WS auth: peer %s has no CA stored — skipping", peer.name)
            continue
        try:
            stored_fp = cert_fingerprint(peer.ca_pem)
            if stored_fp == incoming_fp:
                return peer.name
        except Exception as e:
            log.debug("WS auth: could not fingerprint CA for peer %s: %s", peer.name, e)
            continue
    log.debug("WS auth: no peer matched incoming_fp=%s (peers=%s)",
              incoming_fp[:16], list(state.peers.keys()))
    return None


@sock.route("/agent/ws")
def peer_ws(ws):
    """
    Incoming WebSocket connection from a peer.

    The peer sends its CA PEM in the X-Agent-Ca header during the WS upgrade.
    We verify it matches a known peer's CA fingerprint, then hand the socket
    to the channel manager.
    """
    # Header value is base64-encoded PEM (raw PEM newlines break HTTP headers)
    ca_b64    = request.headers.get("X-Agent-Ca", "")
    try:
        ca_pem = base64.b64decode(ca_b64).decode() if ca_b64 else ""
    except Exception:
        ca_pem = ""
    peer_name = _identify_peer_by_ca(ca_pem)
    if not peer_name:
        log.warning("WS connect rejected: unrecognised CA from %s (name=%s)",
                    request.remote_addr, request.headers.get("X-Agent-Name", "?"))
        # simple_websocket requires a str reason, not bytes
        try:
            ws.close(reason="unauthorized")
        except Exception:
            pass
        return

    log.info("WS channel accepted from peer %s", peer_name)
    from porpulsion.channel import accept_channel
    # accept_channel calls attach_inbound which blocks in this handler thread
    # until the connection closes (simple_websocket requires recv on its own thread).
    accept_channel(peer_name, ws)
    log.info("WS channel closed for peer %s", peer_name)
