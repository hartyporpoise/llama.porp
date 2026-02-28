"""
Porpulsion agent entrypoint.

Initialises runtime config (TLS, invite token, env vars) into the shared
state module, registers Flask blueprints, and starts the HTTP server.
"""
import logging
import os
import pathlib
import socket
import threading

from flask import Flask, render_template

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
    state.SELF_URL = f"http://{_detected_ip}:8000"
    log.warning(
        "SELF_URL not set — auto-detected as %s. "
        "This is a pod-internal IP and will cause peering to fail across clusters. "
        "Set agent.selfUrl in your Helm values to the externally reachable URL for "
        "this agent (e.g. https://porpulsion.example.com).",
        state.SELF_URL
    )

# Load invite token from k8s Secret (generate if absent)
state.invite_token = tls.load_or_generate_token(state.NAMESPACE)

# Load CA cert from k8s Secret (generate if absent).
# The CA cert is exchanged during peering and used to authenticate the WS channel.
_CA_PEM, _CA_KEY_PEM = tls.load_or_generate_ca(state.AGENT_NAME, state.NAMESPACE)

state.AGENT_CA_PEM = _CA_PEM

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

# Re-open WS channels for any peers restored from persistent storage.
# Runs after the Flask app starts (deferred so the WS endpoint is registered).
# Both sides attempt outbound — whichever connects first stays up. If the peer
# also connects inbound simultaneously, accept_channel replaces the outbound
# channel cleanly. This ensures reconnection works regardless of which side
# restarted.
def _reconnect_persisted_peers():
    import time as _time
    _time.sleep(3)  # let the server fully start before connecting outbound
    from porpulsion.channel import open_channel_to
    for _p in state.peers.values():
        log.info("Re-opening WS channel to persisted peer %s", _p.name)
        open_channel_to(_p.name, _p.url, _p.ca_pem)

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

@app.route("/")
@app.route("/ui")
@app.route("/ui/")
@app.route("/ui/<path:_>")
def ui_dashboard(**_):
    return render_template("dashboard.html", agent_name=state.AGENT_NAME)


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
                callback_url = peer.name if peer else ""
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

    threading.Thread(target=_reconstruct_remote_apps, daemon=True).start()
    threading.Thread(target=_reconnect_persisted_peers, daemon=True).start()

    # Peer-facing server (port 8001): /peer and /ws only.
    # This is the port exposed via the Ingress. The dashboard (port 8000)
    # stays internal and is only reachable via port-forward or from inside the cluster.
    from porpulsion.peer_server import start as _start_peer_server
    threading.Thread(target=_start_peer_server, daemon=True, name="peer-server").start()

    app.run(host="0.0.0.0", port=8000, threaded=True)
