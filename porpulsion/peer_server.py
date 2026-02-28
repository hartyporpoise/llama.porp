"""
Peer-facing Flask app (port 8001).

Exposes only the two endpoints that remote peers need to reach:
  POST /peer   — peering handshake
  GET  /ws     — persistent WebSocket channel

Everything else (dashboard, local API) lives on port 8000 and is never
exposed via the Ingress.
"""
import logging

from flask import Flask
from flask_sock import Sock

from porpulsion.routes.peers import accept_peer
from porpulsion.routes.ws import peer_ws

log = logging.getLogger("porpulsion.peer_server")

peer_app = Flask(__name__)

peer_app.add_url_rule("/peer", view_func=accept_peer, methods=["POST"])

sock = Sock(peer_app)
sock.route("/ws")(peer_ws)


def start(port: int = 8001):
    """Start the peer-facing server in the calling thread (run in a daemon thread)."""
    from werkzeug.serving import make_server
    log.info("Starting peer-facing server on port %d", port)
    srv = make_server("0.0.0.0", port, peer_app, threaded=True)
    srv.serve_forever()
