"""
Porpulsion agent entrypoint.

Initialises runtime config (TLS, invite token, env vars) into the shared
state module, registers Flask blueprints, and starts the mTLS + HTTP servers.
"""
import logging
import os
import pathlib
import re
import socket
import ssl
import threading

from flask import Flask, render_template
from werkzeug.serving import make_server, WSGIRequestHandler

from porpulsion import state, tls
from porpulsion.routes import peers as peers_bp
from porpulsion.routes import workloads as workloads_bp
from porpulsion.routes import tunnels as tunnels_bp
from porpulsion.routes import settings as settings_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("porpulsion.agent")

# ── Bootstrap config ──────────────────────────────────────────

state.AGENT_NAME = os.environ.get("AGENT_NAME", "porpulsion-agent")
state.NAMESPACE  = os.environ.get("PORPULSION_NAMESPACE", "porpulsion")

_self_url_env = os.environ.get("SELF_URL", "")
if _self_url_env:
    state.SELF_URL = _self_url_env
else:
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        _detected_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        _detected_ip = "127.0.0.1"
    state.SELF_URL = f"https://{_detected_ip}:8443"

# Extract IP SAN from SELF_URL for leaf cert
_ip_match = re.search(r'https?://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', state.SELF_URL)
SELF_IP = _ip_match.group(1) if _ip_match else ""

# Load invite token from k8s Secret (generate if absent)
state.invite_token = tls.load_or_generate_token(state.NAMESPACE)

# Load CA + leaf cert from k8s Secret (generate if absent, regenerate leaf if IP changed)
_CA_PEM, _CA_KEY_PEM, _CERT_PEM, _KEY_PEM = tls.load_or_generate_cert(
    state.AGENT_NAME, state.NAMESPACE, self_ip=SELF_IP)

state.AGENT_CA_PEM   = _CA_PEM
state.AGENT_CERT_PATH = tls.write_temp_pem(_CERT_PEM, "agent-cert")
state.AGENT_KEY_PATH  = tls.write_temp_pem(_KEY_PEM, "agent-key")

# Expose in tls module for modules that read at call time
tls.AGENT_CERT_PATH = state.AGENT_CERT_PATH
tls.AGENT_KEY_PATH  = state.AGENT_KEY_PATH

log.info("Agent cert written to %s", state.AGENT_CERT_PATH)
log.info("SELF_URL=%s", state.SELF_URL)

# ── Restore persisted state ───────────────────────────────────

from porpulsion.models import Peer, RemoteApp, RemoteAppSpec  # noqa: E402

for _p in tls.load_peers(state.NAMESPACE):
    state.peers[_p["name"]] = Peer(
        name=_p["name"], url=_p["url"], ca_pem=_p.get("ca_pem", ""))

_saved = tls.load_state_configmap(state.NAMESPACE)
for _a in _saved.get("local_apps", []):
    _ra = RemoteApp(
        id=_a["id"], name=_a["name"],
        spec=RemoteAppSpec.from_dict(_a.get("spec", {})),
        source_peer=_a.get("source_peer", ""), target_peer=_a.get("target_peer", ""),
        status=_a.get("status", "Unknown"),
        created_at=_a.get("created_at", ""), updated_at=_a.get("updated_at", ""),
    )
    state.local_apps[_ra.id] = _ra
if "settings" in _saved:
    for _k, _v in _saved["settings"].items():
        if hasattr(state.settings, _k):
            setattr(state.settings, _k, _v)
for _entry in _saved.get("pending_approval", []):
    if _entry.get("id"):
        state.pending_approval[_entry["id"]] = _entry

log.info("Restored %d peer(s), %d local app(s), %d pending approval(s) from persistent storage",
         len(state.peers), len(state.local_apps), len(state.pending_approval))

# ── Flask app ─────────────────────────────────────────────────

_TEMPLATES = pathlib.Path(__file__).parent.parent / "templates"
_STATIC    = pathlib.Path(__file__).parent.parent / "static"
app = Flask(__name__,
            template_folder=str(_TEMPLATES),
            static_folder=str(_STATIC),
            static_url_path="/static")

app.register_blueprint(peers_bp.bp)
app.register_blueprint(workloads_bp.bp)
app.register_blueprint(tunnels_bp.bp)
app.register_blueprint(settings_bp.bp)

# Re-register peer-facing blueprints under /agent so the mTLS port (8443)
# has a dedicated path that nginx can route separately from the plain HTTP
# dashboard (8000). Peer agents call https://<self_url>/agent/... over mTLS;
# the plain HTTP port serves the dashboard at its normal paths.
app.register_blueprint(peers_bp.bp,     url_prefix="/agent", name="peers_agent")
app.register_blueprint(workloads_bp.bp, url_prefix="/agent", name="workloads_agent")
app.register_blueprint(tunnels_bp.bp,   url_prefix="/agent", name="tunnels_agent")


@app.route("/")
@app.route("/ui")
@app.route("/ui/")
@app.route("/ui/<path:_>")
def ui_dashboard(**_):
    return render_template("dashboard.html", agent_name=state.AGENT_NAME)


# ── mTLS server management ────────────────────────────────────

_mtls_server = None
_mtls_lock   = threading.Lock()


class _MTLSRequestHandler(WSGIRequestHandler):
    """
    Werkzeug request handler that injects SSL_CLIENT_CERT into the WSGI environ.

    Werkzeug's make_server does not expose the peer certificate via the WSGI
    environ by default. We extract it from the underlying ssl.SSLSocket after
    the handshake and inject it as SSL_CLIENT_CERT (PEM string) so that
    verify_peer() in peering.py can authenticate peer-to-peer API calls.
    """

    def make_environ(self):
        environ = super().make_environ()
        try:
            ssl_sock = self.connection
            der = ssl_sock.getpeercert(binary_form=True)
            if der:
                import ssl as _ssl
                pem = _ssl.DER_cert_to_PEM_cert(der)
                environ["SSL_CLIENT_CERT"] = pem
        except Exception:
            pass
        return environ


def rebuild_mtls_server():
    """
    Rebuild the mTLS SSL context with the current peer CA bundle.

    Runs in a background thread to avoid deadlocking when called from within
    a request handler (shutdown() waits for in-flight requests).
    """
    peer_ca_pems = [
        p.ca_pem.encode() if isinstance(p.ca_pem, str) else p.ca_pem
        for p in state.peers.values() if p.ca_pem
    ]
    ssl_ctx = tls.make_server_ssl_context(
        state.AGENT_CERT_PATH, state.AGENT_KEY_PATH, peer_ca_pems=peer_ca_pems)

    def _swap():
        global _mtls_server
        with _mtls_lock:
            old = _mtls_server
            _mtls_server = None
        if old is not None:
            old.shutdown()
        import time as _time
        for _ in range(10):
            try:
                new_server = make_server(
                    "0.0.0.0", 8443, app,
                    ssl_context=ssl_ctx, threaded=True,
                    request_handler=_MTLSRequestHandler,
                )
                with _mtls_lock:
                    _mtls_server = new_server
                threading.Thread(target=new_server.serve_forever, daemon=True).start()
                log.info("mTLS context rebuilt (%d peer CA(s) trusted)", len(peer_ca_pems))
                return
            except OSError:
                _time.sleep(0.3)
        log.error("Failed to rebind mTLS server on port 8443 after shutdown")

    threading.Thread(target=_swap, daemon=True).start()


# Wire the rebuild callback into state so blueprints can call it
state._rebuild_mtls_callback = rebuild_mtls_server


def _reconstruct_remote_apps():
    """
    Rebuild state.remote_apps from live k8s Deployments after a restart.

    Lists all Deployments in the porpulsion namespace labelled with
    porpulsion.io/remote-app-id, reconstructs a minimal RemoteApp for each,
    and sets status based on ready replicas. Skips IDs already in state.
    Runs as a daemon thread so it doesn't block startup.
    """
    try:
        from kubernetes import client as _k8s, config as _kube_config
        try:
            _kube_config.load_incluster_config()
        except Exception:
            _kube_config.load_kube_config()
        apps_v1 = _k8s.AppsV1Api()
        deploys = apps_v1.list_namespaced_deployment(
            state.NAMESPACE,
            label_selector="porpulsion.io/remote-app-id",
        )
        restored = 0
        for dep in deploys.items:
            labels = dep.metadata.labels or {}
            app_id      = labels.get("porpulsion.io/remote-app-id", "")
            source_peer = labels.get("porpulsion.io/source-peer", "unknown")
            if not app_id or app_id in state.remote_apps:
                continue
            ready   = dep.status.ready_replicas or 0
            desired = dep.spec.replicas or 1
            # Reconstruct name from deploy_name: "ra-{id}-{name}" → strip prefix
            deploy_name = dep.metadata.name
            name = deploy_name[len(f"ra-{app_id}-"):] if deploy_name.startswith(f"ra-{app_id}-") else deploy_name
            already_ready = ready >= desired
            ra = RemoteApp(
                id=app_id, name=name, spec=RemoteAppSpec(image="", replicas=desired),
                source_peer=source_peer,
                status="Ready" if already_ready else "Running",
            )
            state.remote_apps[app_id] = ra

            # If not yet ready, find the source peer and resume polling so the
            # status will eventually transition to Ready.
            if not already_ready:
                peer = state.peers.get(source_peer)
                callback_url = peer.url if peer else ""
                from porpulsion.k8s.executor import run_workload
                # run_workload restarts the deployment — we only want to resume
                # the status watcher. Kick a lightweight watcher thread instead.
                def _watch(ra=ra, callback_url=callback_url, peer=peer, desired=desired):
                    from porpulsion.k8s import executor as _ex
                    import time as _time
                    deploy_nm = f"ra-{ra.id}-{ra.name}"[:63]
                    for _ in range(60):
                        _time.sleep(2)
                        try:
                            d = _ex.apps_v1.read_namespaced_deployment_status(deploy_nm, state.NAMESPACE)
                            if (d.status.ready_replicas or 0) >= desired:
                                _ex._report_status(ra, callback_url, "Ready", peer=peer)
                                return
                        except Exception:
                            pass
                    _ex._report_status(ra, callback_url, "Timeout", peer=peer)
                threading.Thread(target=_watch, daemon=True).start()

            restored += 1
        log.info("Reconstructed %d remote app(s) from k8s Deployments", restored)
    except Exception as exc:
        log.warning("Could not reconstruct remote_apps from k8s: %s", exc)


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting agent %s", state.AGENT_NAME)

    level = getattr(logging, state.settings.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)

    rebuild_mtls_server()
    log.info("mTLS listener started on port 8443")

    threading.Thread(target=_reconstruct_remote_apps, daemon=True).start()

    app.run(host="0.0.0.0", port=8000)
