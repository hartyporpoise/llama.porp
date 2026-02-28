"""
Shared in-memory state for the porpulsion agent.

All route modules import from here so they share the same live dicts.
Config constants (AGENT_NAME, SELF_URL, etc.) are set once at startup
by porpulsion/agent.py and read by routes at call time.
"""
from porpulsion.models import Peer, RemoteApp, TunnelRequest, AgentSettings

# ── Runtime config (set by agent.py at startup) ──────────────
AGENT_NAME: str = ""
NAMESPACE:  str = "porpulsion"
SELF_URL:   str = ""
AGENT_CA_PEM: bytes = b""
AGENT_CERT_PATH: str = ""
AGENT_KEY_PATH:  str = ""

# ── In-memory state ───────────────────────────────────────────
peers:          dict[str, Peer]          = {}
pending_peers:  dict[str, dict]          = {}   # url  -> {name, url, since, attempts, status, ca_pem}
pending_inbound: dict[str, dict]         = {}   # id   -> {name, url, ca_pem, since}
local_apps:     dict[str, RemoteApp]     = {}   # apps we submitted, tracked locally
remote_apps:    dict[str, RemoteApp]     = {}   # apps received from peers, executing here
pending_approval: dict[str, dict]        = {}   # id -> {id, name, spec, source_peer, callback_url, since}
tunnel_requests: dict[str, TunnelRequest] = {}  # pending/approved/rejected tunnel requests
settings: AgentSettings = AgentSettings()
invite_token: str = ""

# Callback set by agent.py so route blueprints can trigger mTLS server rebuilds
_rebuild_mtls_callback = None
