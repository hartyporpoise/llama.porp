"""
HTTP proxy helper for porpulsion RemoteApp port forwarding.

Provides `proxy_request` — used by the executing agent to forward an
inbound HTTP request (received from the submitting agent over mTLS) to
the correct pod running a RemoteApp, then stream the response back.

Scope enforcement: pod IP is resolved fresh from k8s at call time using
the porpulsion.io/remote-app-id label, so the caller never supplies a
target address directly.
"""
import logging

log = logging.getLogger("porpulsion.tunnel")

import os
NAMESPACE = os.environ.get("PORPULSION_NAMESPACE", "porpulsion")


def _k8s_core_v1():
    from kubernetes import client, config as kube_config
    try:
        kube_config.load_incluster_config()
    except Exception:
        kube_config.load_kube_config()
    return client.CoreV1Api()


def resolve_pod_ip(remote_app_id: str) -> str:
    """
    Look up the IP of a running pod owned by remote_app_id.
    Raises ValueError if no running pod is found.
    """
    core_v1 = _k8s_core_v1()
    pods = core_v1.list_namespaced_pod(
        namespace=NAMESPACE,
        label_selector=f"porpulsion.io/remote-app-id={remote_app_id}",
    )
    running = [p for p in pods.items if p.status.phase == "Running" and p.status.pod_ip]
    if not running:
        raise ValueError(f"no running pods for remote-app-id={remote_app_id}")
    return running[0].status.pod_ip


def proxy_request(remote_app_id: str, port: int,
                  method: str, path: str,
                  headers: dict, body: bytes) -> tuple[int, dict, bytes]:
    """
    Forward an HTTP request to a pod running a RemoteApp.

    Returns (status_code, response_headers, response_body).
    The pod IP is resolved fresh from k8s to enforce scope.
    """
    import requests as _req

    pod_ip = resolve_pod_ip(remote_app_id)
    url = f"http://{pod_ip}:{port}/{path.lstrip('/')}"

    # Strip hop-by-hop headers that must not be forwarded
    _skip = {"host", "transfer-encoding", "connection", "keep-alive",
              "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}
    fwd_headers = {k: v for k, v in headers.items() if k.lower() not in _skip}

    try:
        resp = _req.request(
            method=method,
            url=url,
            headers=fwd_headers,
            data=body,
            timeout=30,
            allow_redirects=False,
            stream=False,
        )
        resp_headers = {k: v for k, v in resp.headers.items()
                        if k.lower() not in _skip}
        log.debug("Proxied %s %s -> %s: %d", method, path, url, resp.status_code)
        return resp.status_code, resp_headers, resp.content
    except Exception as exc:
        log.warning("Proxy error for app %s port %d: %s", remote_app_id, port, exc)
        raise
