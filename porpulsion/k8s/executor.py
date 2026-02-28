import logging
import os
import threading
import time
import requests as http_requests
from datetime import datetime, timezone
from kubernetes import client, config

log = logging.getLogger("porpulsion.executor")

# Load in-cluster kubeconfig (the agent runs as a pod)
try:
    config.load_incluster_config()
except config.ConfigException:
    log.warning("Not running in-cluster, falling back to default kubeconfig")
    config.load_kube_config()

apps_v1 = client.AppsV1Api()
core_v1 = client.CoreV1Api()

NAMESPACE = os.environ.get("PORPULSION_NAMESPACE", "porpulsion")

# Tracks the active polling thread stop-event per app id so re-deploys
# cancel the old watcher before starting a new one.
_stop_events: dict[str, threading.Event] = {}


def _peer_session(peer=None):
    """Build a requests.Session with mTLS client cert and peer CA verification."""
    from porpulsion import tls
    session = http_requests.Session()
    session.cert = (tls.AGENT_CERT_PATH, tls.AGENT_KEY_PATH)
    session.verify = tls.peer_ca_path(peer.name) if (peer and peer.ca_pem) else False
    return session


def _report_status(remote_app, callback_url, status, peer=None, retries=3):
    """Report status back to the originating peer via mTLS. Retries on transient failure."""
    remote_app.status = status
    remote_app.updated_at = datetime.now(timezone.utc).isoformat()
    log.info("App %s (%s) -> %s", remote_app.name, remote_app.id, status)
    if not callback_url:
        return
    payload = {"status": status, "updated_at": remote_app.updated_at}
    for attempt in range(retries):
        try:
            session = _peer_session(peer)
            resp = session.post(
                f"{callback_url}/agent/remoteapp/{remote_app.id}/status",
                json=payload,
                timeout=5,
            )
            if resp.ok:
                return
            log.warning("Status callback got %s (attempt %d)", resp.status_code, attempt + 1)
        except Exception as e:
            log.warning("Failed to report status to %s (attempt %d): %s", callback_url, attempt + 1, e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s backoff


def run_workload(remote_app, callback_url, peer=None):
    """Create a real Kubernetes Deployment for the RemoteApp."""
    # Cancel any existing watcher for this app before starting a new one
    existing = _stop_events.get(remote_app.id)
    if existing:
        existing.set()
    stop = threading.Event()
    _stop_events[remote_app.id] = stop

    def _execute():
        spec     = remote_app.spec
        image    = spec.image
        replicas = spec.replicas
        deploy_name = f"ra-{remote_app.id}-{remote_app.name}"[:63]

        # ── resources ────────────────────────────────────────
        resource_requirements = None
        if not spec.resources.is_empty():
            resource_requirements = client.V1ResourceRequirements(
                requests=spec.resources.requests or None,
                limits=spec.resources.limits or None,
            )

        # ── ports ─────────────────────────────────────────────
        if spec.ports:
            container_ports = [
                client.V1ContainerPort(
                    container_port=p.port,
                    name=(p.name[:15] if p.name else f"port-{p.port}"),
                )
                for p in spec.ports
            ]
        else:
            container_ports = [client.V1ContainerPort(
                container_port=spec.port or 80
            )]

        # ── env ─────────────────────────────────────────────
        env_list = None
        if spec.env:
            env_list = []
            for e in spec.env:
                if e.valueFrom:
                    vf = e.valueFrom
                    if vf.secretKeyRef:
                        ref = vf.secretKeyRef
                        env_list.append(client.V1EnvVar(
                            name=e.name,
                            value_from=client.V1EnvVarSource(
                                secret_key_ref=client.V1SecretKeySelector(
                                    name=ref["name"], key=ref["key"]
                                )
                            ),
                        ))
                    elif vf.configMapKeyRef:
                        ref = vf.configMapKeyRef
                        env_list.append(client.V1EnvVar(
                            name=e.name,
                            value_from=client.V1EnvVarSource(
                                config_map_key_ref=client.V1ConfigMapKeySelector(
                                    name=ref["name"], key=ref["key"]
                                )
                            ),
                        ))
                else:
                    env_list.append(client.V1EnvVar(name=e.name, value=e.value))

        # ── imagePullPolicy / imagePullSecrets ───────────────
        pull_policy = spec.imagePullPolicy
        pull_secrets = [client.V1LocalObjectReference(name=s) for s in spec.imagePullSecrets] \
            if spec.imagePullSecrets else None

        # ── command / args ───────────────────────────────────
        command = spec.command or None
        args    = spec.args    or None

        # ── readinessProbe ───────────────────────────────────
        readiness_probe = None
        rp = spec.readinessProbe
        if rp:
            http_get = None
            exec_action = None
            if rp.httpGet:
                http_get = client.V1HTTPGetAction(
                    path=rp.httpGet.get("path", "/"),
                    port=rp.httpGet.get("port", 80),
                )
            elif rp.exec:
                exec_action = client.V1ExecAction(command=rp.exec.get("command", []))
            readiness_probe = client.V1Probe(
                http_get=http_get,
                _exec=exec_action,
                initial_delay_seconds=rp.initialDelaySeconds,
                period_seconds=rp.periodSeconds,
                failure_threshold=rp.failureThreshold,
            )

        # ── securityContext ──────────────────────────────────
        pod_security_ctx = None
        container_security_ctx = None
        sc = spec.securityContext
        if sc:
            pod_security_ctx = client.V1PodSecurityContext(
                run_as_non_root=sc.runAsNonRoot,
                run_as_user=sc.runAsUser,
                run_as_group=sc.runAsGroup,
                fs_group=sc.fsGroup,
            )
            if sc.readOnlyRootFilesystem is not None:
                container_security_ctx = client.V1SecurityContext(
                    read_only_root_filesystem=sc.readOnlyRootFilesystem
                )

        try:
            core_v1.read_namespace(NAMESPACE)
        except client.ApiException:
            core_v1.create_namespace(
                client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
            )

        _report_status(remote_app, callback_url, "Creating", peer=peer)

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=deploy_name,
                namespace=NAMESPACE,
                labels={
                    "app": deploy_name,
                    "porpulsion.io/remote-app-id": remote_app.id,
                    "porpulsion.io/source-peer": remote_app.source_peer,
                },
            ),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector=client.V1LabelSelector(match_labels={"app": deploy_name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app": deploy_name,
                            "porpulsion.io/remote-app-id": remote_app.id,
                        },
                    ),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="main",
                                image=image,
                                image_pull_policy=pull_policy,
                                command=command,
                                args=args,
                                ports=container_ports,
                                resources=resource_requirements,
                                env=env_list,
                                readiness_probe=readiness_probe,
                                security_context=container_security_ctx,
                            )
                        ],
                        image_pull_secrets=pull_secrets,
                        security_context=pod_security_ctx,
                    ),
                ),
            ),
        )

        try:
            apps_v1.create_namespaced_deployment(namespace=NAMESPACE, body=deployment)
            log.info("Created deployment %s in %s", deploy_name, NAMESPACE)
        except client.ApiException as e:
            if e.status == 409:
                log.info("Deployment %s already exists, updating", deploy_name)
                apps_v1.replace_namespaced_deployment(
                    name=deploy_name, namespace=NAMESPACE, body=deployment
                )
            else:
                _report_status(remote_app, callback_url, f"Failed: {e.reason}", peer=peer)
                return

        _report_status(remote_app, callback_url, "Running", peer=peer)

        for _ in range(60):
            if stop.is_set():
                log.info("Watcher for %s cancelled (re-deploy)", remote_app.id)
                return
            time.sleep(2)
            try:
                dep = apps_v1.read_namespaced_deployment_status(deploy_name, NAMESPACE)
                ready = dep.status.ready_replicas or 0
                if ready >= replicas:
                    _report_status(remote_app, callback_url, "Ready", peer=peer)
                    _stop_events.pop(remote_app.id, None)
                    return
            except client.ApiException as e:
                log.warning("Error checking deployment status: %s", e.reason)

        _report_status(remote_app, callback_url, "Timeout", peer=peer)
        _stop_events.pop(remote_app.id, None)

    t = threading.Thread(target=_execute, daemon=True)
    t.start()


def delete_workload(remote_app) -> None:
    """Delete the Kubernetes Deployment for a RemoteApp."""
    deploy_name = f"ra-{remote_app.id}-{remote_app.name}"[:63]
    try:
        apps_v1.delete_namespaced_deployment(
            name=deploy_name,
            namespace=NAMESPACE,
            body=client.V1DeleteOptions(propagation_policy="Foreground"),
        )
        log.info("Deleted deployment %s", deploy_name)
    except client.ApiException as e:
        if e.status == 404:
            log.info("Deployment %s already gone", deploy_name)
        else:
            log.warning("Error deleting deployment %s: %s", deploy_name, e.reason)


def scale_workload(remote_app, replicas: int) -> None:
    """Scale a RemoteApp deployment to the given replica count."""
    deploy_name = f"ra-{remote_app.id}-{remote_app.name}"[:63]
    try:
        dep = apps_v1.read_namespaced_deployment(deploy_name, NAMESPACE)
        dep.spec.replicas = replicas
        apps_v1.replace_namespaced_deployment(deploy_name, NAMESPACE, dep)
        log.info("Scaled deployment %s to %d replicas", deploy_name, replicas)
    except client.ApiException as e:
        log.warning("Error scaling deployment %s: %s", deploy_name, e.reason)
        raise


def get_deployment_status(remote_app) -> dict:
    """Return live k8s status info for a RemoteApp deployment."""
    deploy_name = f"ra-{remote_app.id}-{remote_app.name}"[:63]
    try:
        dep = apps_v1.read_namespaced_deployment_status(deploy_name, NAMESPACE)
        pods = core_v1.list_namespaced_pod(
            NAMESPACE,
            label_selector=f"porpulsion.io/remote-app-id={remote_app.id}",
        )
        pod_list = []
        for p in pods.items:
            pod_list.append({
                "name": p.metadata.name,
                "phase": p.status.phase,
                "ready": all(c.ready for c in (p.status.container_statuses or [])),
                "restarts": sum(c.restart_count for c in (p.status.container_statuses or [])),
                "node": p.spec.node_name,
            })
        return {
            "deploy_name": deploy_name,
            "desired": dep.spec.replicas,
            "ready": dep.status.ready_replicas or 0,
            "available": dep.status.available_replicas or 0,
            "updated": dep.status.updated_replicas or 0,
            "pods": pod_list,
        }
    except client.ApiException as e:
        if e.status == 404:
            return {"error": "deployment not found"}
        raise
