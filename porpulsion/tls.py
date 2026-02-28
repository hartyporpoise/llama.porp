"""
TLS certificate generation and management for porpulsion agents.

Each agent auto-generates a private CA on first boot, persisted to the
porpulsion-credentials Kubernetes Secret. A leaf cert signed by that CA
is used for the mTLS listener. During peering the CA cert (not the leaf)
is exchanged — peers store each other's CA and use it as the trust anchor
for all subsequent mTLS connections. This gives full mutual authentication
with no external dependencies and works on private networks.
"""
import base64
import os
import ssl
import datetime
import ipaddress
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_ca_and_leaf_cert(agent_name: str,
                               self_ip: str = "") -> tuple[bytes, bytes, bytes, bytes]:
    """
    Generate a private CA and a leaf cert signed by it.

    The CA cert is long-lived (10 years) and is what peers exchange during
    the peering handshake. The leaf cert is used on the mTLS listener and
    can be rotated independently without re-peering.

    self_ip: included as an IP SAN in the leaf cert so peers connecting
    by bare IP pass TLS hostname verification.

    Returns (ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem) as bytes.
    """
    # ── CA key + self-signed CA cert ──────────────────────────
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{agent_name}-ca"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "porpulsion"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # ── Leaf key + cert signed by the CA ──────────────────────
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, agent_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "porpulsion"),
    ])
    san_entries: list = [x509.DNSName(agent_name)]
    if self_ip:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(self_ip)))
        except ValueError:
            pass
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        ]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    def _pem(obj):
        return obj.public_bytes(serialization.Encoding.PEM)

    def _key_pem(k):
        return k.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

    return _pem(ca_cert), _key_pem(ca_key), _pem(leaf_cert), _key_pem(leaf_key)


def write_temp_pem(pem_bytes: bytes, name: str) -> str:
    """Write PEM bytes to /tmp/porpulsion-{name}.pem and return the path."""
    path = f"/tmp/porpulsion-{name}.pem"
    with open(path, "wb") as f:
        f.write(pem_bytes)
    os.chmod(path, 0o600)
    return path


def cert_fingerprint(cert_pem: str | bytes) -> str:
    """Return the SHA-256 hex fingerprint of a PEM-encoded certificate."""
    if isinstance(cert_pem, str):
        cert_pem = cert_pem.encode()
    from cryptography.x509 import load_pem_x509_certificate
    cert = load_pem_x509_certificate(cert_pem)
    return cert.fingerprint(hashes.SHA256()).hex()


def make_server_ssl_context(cert_path: str, key_path: str,
                             peer_ca_pems: list[bytes] | None = None) -> ssl.SSLContext:
    """
    Build an SSL context for the agent-to-agent HTTPS listener (port 8443).

    Uses CERT_REQUIRED — every connecting peer must present a client cert
    signed by a CA we trust. peer_ca_pems is the list of CA certs from all
    currently connected peers; the context is rebuilt whenever a new peer
    is added (see rebuild_mtls_server in agent.py).

    With no peer CAs yet (first boot before any peering), CERT_NONE is used
    temporarily so the peering bootstrap requests can reach us.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    if peer_ca_pems:
        ctx.verify_mode = ssl.CERT_REQUIRED
        # Write all peer CA certs into one bundle file and load it
        bundle = b"".join(peer_ca_pems)
        bundle_path = write_temp_pem(bundle, "peer-ca-bundle")
        ctx.load_verify_locations(cafile=bundle_path)
    else:
        # No peers yet — accept unauthenticated connections for bootstrap
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_ca_path(peer_name: str) -> str:
    """Return the /tmp path where a peer's CA cert is stored."""
    return f"/tmp/porpulsion-peer-ca-{peer_name}.pem"


# Module-level placeholders — set by agent.py at startup after generating certs.
AGENT_CERT_PATH: str = ""
AGENT_KEY_PATH: str = ""

_CREDENTIALS_SECRET = "porpulsion-credentials"


def _k8s_core_v1():
    """Return a CoreV1Api client, loading config lazily."""
    from kubernetes import client, config as kube_config
    try:
        kube_config.load_incluster_config()
    except Exception:
        kube_config.load_kube_config()
    return client.CoreV1Api()


def _save_credentials_secret(core_v1, namespace: str,
                              ca_cert_pem: bytes | None = None,
                              ca_key_pem: bytes | None = None,
                              cert_pem: bytes | None = None,
                              key_pem: bytes | None = None,
                              invite_token: str | None = None,
                              self_ip: str | None = None,
                              peers_json: str | None = None) -> None:
    """
    Create or patch the porpulsion-credentials Secret with any non-None fields.
    """
    from kubernetes import client as k8s_client
    data = {}
    if ca_cert_pem is not None:
        data["ca.crt"] = base64.b64encode(ca_cert_pem).decode()
    if ca_key_pem is not None:
        data["ca.key"] = base64.b64encode(ca_key_pem).decode()
    if cert_pem is not None:
        data["tls.crt"] = base64.b64encode(cert_pem).decode()
    if key_pem is not None:
        data["tls.key"] = base64.b64encode(key_pem).decode()
    if invite_token is not None:
        data["invite-token"] = base64.b64encode(invite_token.encode()).decode()
    if self_ip is not None:
        data["self-ip"] = base64.b64encode(self_ip.encode()).decode()
    if peers_json is not None:
        data["peers"] = base64.b64encode(peers_json.encode()).decode()

    if not data:
        return

    secret = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(name=_CREDENTIALS_SECRET, namespace=namespace),
        data=data,
    )
    try:
        core_v1.create_namespaced_secret(namespace, secret)
    except k8s_client.ApiException as e:
        if e.status == 409:
            core_v1.patch_namespaced_secret(_CREDENTIALS_SECRET, namespace, secret)
        else:
            raise


def load_or_generate_cert(agent_name: str, namespace: str,
                           self_ip: str = "") -> tuple[bytes, bytes, bytes, bytes]:
    """
    Load CA + leaf cert/key from the porpulsion-credentials Secret, or generate
    them fresh if missing. Regenerates the leaf cert (but not the CA) if the
    agent's IP has changed, preserving the CA fingerprint so existing peers
    remain valid.

    Returns (ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem) as bytes.
    """
    import logging
    log = logging.getLogger("porpulsion.tls")
    core_v1 = _k8s_core_v1()

    try:
        secret = core_v1.read_namespaced_secret(_CREDENTIALS_SECRET, namespace)
        d = secret.data or {}
        if all(k in d for k in ("ca.crt", "ca.key", "tls.crt", "tls.key")):
            ca_cert_pem = base64.b64decode(d["ca.crt"])
            ca_key_pem  = base64.b64decode(d["ca.key"])
            stored_ip   = base64.b64decode(d["self-ip"]).decode() if "self-ip" in d else ""
            if stored_ip == self_ip:
                leaf_cert_pem = base64.b64decode(d["tls.crt"])
                leaf_key_pem  = base64.b64decode(d["tls.key"])
                log.info("Loaded existing CA + leaf cert from Secret (IP unchanged)")
                return ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem
            else:
                # IP changed — reuse CA, regenerate only the leaf
                log.info("IP changed (%s → %s) — regenerating leaf cert, preserving CA",
                         stored_ip or "(none)", self_ip or "(none)")
                leaf_cert_pem, leaf_key_pem = _generate_leaf(
                    agent_name, self_ip, ca_cert_pem, ca_key_pem)
                try:
                    _save_credentials_secret(core_v1, namespace,
                                             cert_pem=leaf_cert_pem, key_pem=leaf_key_pem,
                                             self_ip=self_ip)
                except Exception as exc:
                    log.warning("Could not persist new leaf cert: %s", exc)
                return ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem
    except Exception:
        pass  # Secret missing or incomplete — generate everything fresh

    log.info("Generating new CA + leaf cert for %s", agent_name)
    ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem = generate_ca_and_leaf_cert(
        agent_name, self_ip=self_ip)
    try:
        _save_credentials_secret(core_v1, namespace,
                                  ca_cert_pem=ca_cert_pem, ca_key_pem=ca_key_pem,
                                  cert_pem=leaf_cert_pem, key_pem=leaf_key_pem,
                                  self_ip=self_ip)
    except Exception as exc:
        log.warning("Could not persist certs to Secret: %s", exc)
    return ca_cert_pem, ca_key_pem, leaf_cert_pem, leaf_key_pem


def _generate_leaf(agent_name: str, self_ip: str,
                   ca_cert_pem: bytes, ca_key_pem: bytes) -> tuple[bytes, bytes]:
    """Generate a new leaf cert signed by the existing CA."""
    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    ca_cert = load_pem_x509_certificate(ca_cert_pem)
    ca_key_obj = load_pem_private_key(ca_key_pem, password=None)

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, agent_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "porpulsion"),
    ])
    san_entries: list = [x509.DNSName(agent_name)]
    if self_ip:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(self_ip)))
        except ValueError:
            pass
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.ExtendedKeyUsage([
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        ]), critical=False)
        .sign(ca_key_obj, hashes.SHA256())
    )
    cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
    key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def load_or_generate_token(namespace: str) -> str:
    """
    Try to load the invite token from the porpulsion-credentials Secret.
    If absent, generate a fresh one and save it back.
    """
    import logging
    import secrets as _secrets
    core_v1 = _k8s_core_v1()
    try:
        secret = core_v1.read_namespaced_secret(_CREDENTIALS_SECRET, namespace)
        if secret.data and "invite-token" in secret.data:
            token = base64.b64decode(secret.data["invite-token"]).decode()
            if token:
                return token
    except Exception:
        pass

    token = _secrets.token_hex(32)
    try:
        _save_credentials_secret(core_v1, namespace, invite_token=token)
    except Exception as exc:
        logging.getLogger("porpulsion.tls").warning(
            "Could not persist invite token to Secret: %s", exc
        )
    return token


def persist_token(namespace: str, token: str) -> None:
    """Write a rotated invite token back to the credentials Secret (fire-and-forget)."""
    import threading
    def _write():
        try:
            core_v1 = _k8s_core_v1()
            _save_credentials_secret(core_v1, namespace, invite_token=token)
        except Exception as exc:
            import logging
            logging.getLogger("porpulsion.tls").warning(
                "Could not persist rotated token to Secret: %s", exc
            )
    threading.Thread(target=_write, daemon=True).start()


# ── Peer persistence ──────────────────────────────────────────

def save_peers(namespace: str, peers: dict) -> None:
    """
    Persist the peers dict to the porpulsion-credentials Secret (fire-and-forget thread).
    Serialises each peer as {name, url, ca_pem}.
    """
    import json
    import threading
    import logging
    _log = logging.getLogger("porpulsion.tls")

    peer_list = [
        {"name": p.name, "url": p.url, "ca_pem": p.ca_pem}
        for p in peers.values()
    ]
    json_str = json.dumps(peer_list)

    def _write():
        try:
            core_v1 = _k8s_core_v1()
            _save_credentials_secret(core_v1, namespace, peers_json=json_str)
            _log.debug("Persisted %d peer(s) to Secret", len(peer_list))
        except Exception as exc:
            _log.warning("Could not persist peers to Secret: %s", exc)

    threading.Thread(target=_write, daemon=True).start()


def load_peers(namespace: str) -> list[dict]:
    """
    Load the peers list from the porpulsion-credentials Secret.
    Also re-writes each peer's CA PEM to /tmp so mTLS verify paths are ready.
    Returns [] on missing Secret or any error.
    """
    import json
    import logging
    _log = logging.getLogger("porpulsion.tls")
    try:
        core_v1 = _k8s_core_v1()
        secret = core_v1.read_namespaced_secret(_CREDENTIALS_SECRET, namespace)
        if not (secret.data and "peers" in secret.data):
            return []
        peer_list = json.loads(base64.b64decode(secret.data["peers"]).decode())
        for p in peer_list:
            if p.get("ca_pem"):
                write_temp_pem(
                    p["ca_pem"].encode() if isinstance(p["ca_pem"], str) else p["ca_pem"],
                    f"peer-ca-{p['name']}",
                )
        _log.info("Loaded %d peer(s) from Secret", len(peer_list))
        return peer_list
    except Exception as exc:
        _log.warning("Could not load peers from Secret: %s", exc)
        return []


# ── State ConfigMap (local_apps + settings) ───────────────────

_STATE_CONFIGMAP = "porpulsion-state"


def save_state_configmap(namespace: str, local_apps: dict, settings) -> None:
    """
    Persist local_apps list and settings to the porpulsion-state ConfigMap
    (fire-and-forget thread).
    """
    import json
    import threading
    import logging
    from kubernetes import client as k8s_client
    _log = logging.getLogger("porpulsion.tls")

    apps_json     = json.dumps([a.to_dict() for a in local_apps.values()])
    settings_json = json.dumps(settings.to_dict())

    def _write():
        try:
            core_v1 = _k8s_core_v1()
            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(
                    name=_STATE_CONFIGMAP, namespace=namespace),
                data={"local_apps": apps_json, "settings": settings_json},
            )
            try:
                core_v1.create_namespaced_config_map(namespace, cm)
            except k8s_client.ApiException as e:
                if e.status == 409:
                    core_v1.patch_namespaced_config_map(_STATE_CONFIGMAP, namespace, cm)
                else:
                    raise
            _log.debug("Persisted %d local app(s) + settings to ConfigMap",
                       len(local_apps))
        except Exception as exc:
            _log.warning("Could not persist state to ConfigMap: %s", exc)

    threading.Thread(target=_write, daemon=True).start()


def load_state_configmap(namespace: str) -> dict:
    """
    Load local_apps list and settings from the porpulsion-state ConfigMap.
    Returns {"local_apps": [...], "settings": {...}} or {} on missing/error.
    """
    import json
    import logging
    _log = logging.getLogger("porpulsion.tls")
    try:
        core_v1 = _k8s_core_v1()
        cm = core_v1.read_namespaced_config_map(_STATE_CONFIGMAP, namespace)
        result = {}
        if cm.data and "local_apps" in cm.data:
            result["local_apps"] = json.loads(cm.data["local_apps"])
        if cm.data and "settings" in cm.data:
            result["settings"] = json.loads(cm.data["settings"])
        _log.info("Loaded %d local app(s) + settings from ConfigMap",
                  len(result.get("local_apps", [])))
        return result
    except Exception as exc:
        _log.warning("Could not load state from ConfigMap: %s", exc)
        return {}
