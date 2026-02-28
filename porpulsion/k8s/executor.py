import logging
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

import os
NAMESPACE = os.environ.get("PORPULSION_NAMESPACE", "porpulsion")


def _peer_session(peer=None):
    """Build a requests.Session with mTLS client cert and peer CA verification."""
    from porpulsion import tls
    session = http_requests.Session()
    session.cert = (tls.AGENT_CERT_PATH, tls.AGENT_KEY_PATH)
    session.verify = tls.peer_ca_path(peer.name) if (peer and peer.ca_pem) else False
    return session


def _report_status(remote_app, callback_url, status, peer=None):
    """Report status back to the originating peer via mTLS."""
    remote_app.status = status
    remote_app.updated_at = datetime.now(timezone.utc).isoformat()
    log.info("App %s (%s) -> %s", remote_app.name, remote_app.id, status)
    if callback_url:
        try:
            session = _peer_session(peer)
            session.post(
                f"{callback_url}/remoteapp/{remote_app.id}/status",
                json={"status": status, "updated_at": remote_app.updated_at},
                timeout=5,
            )
        except Exception as e:
            log.warning("Failed to report status to %s: %s", callback_url, e)


def run_workload(remote_app, callback_url, peer=None):
    """Create a real Kubernetes Deployment for the RemoteApp."""

    def _execute():
        image    = remote_app.spec.get("image", "nginx:latest")
        replicas = remote_app.spec.get("replicas", 1)
        deploy_name = f"ra-{remote_app.id}-{remote_app.name}"[:63]

        cpu_val = remote_app.spec.get("cpu")
        mem_val = remote_app.spec.get("memory_mb")
        resource_requirements = None
        if cpu_val or mem_val:
            req = {}
            lim = {}
            if cpu_val is not None:
                cpu_str = f"{int(float(cpu_val) * 1000)}m"
                req["cpu"] = cpu_str
                lim["cpu"] = cpu_str
            if mem_val is not None:
                mem_str = f"{int(mem_val)}Mi"
                req["memory"] = mem_str
                lim["memory"] = mem_str
            resource_requirements = client.V1ResourceRequirements(requests=req, limits=lim)

        # Build container port list from spec.
        # Supports two formats:
        #   ports: [{port: 80, name: http}, {port: 9090, name: metrics}]  (preferred)
        #   port: 80  (legacy single-port shorthand)
        spec_ports = remote_app.spec.get("ports")
        if spec_ports and isinstance(spec_ports, list):
            container_ports = [
                client.V1ContainerPort(
                    container_port=int(p["port"]),
                    name=str(p.get("name", f"port-{p['port']}"))[:15],
                )
                for p in spec_ports if p.get("port")
            ] or [client.V1ContainerPort(container_port=80)]
        else:
            container_ports = [client.V1ContainerPort(
                container_port=int(remote_app.spec.get("port", 80) or 80)
            )]

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
                                ports=container_ports,
                                resources=resource_requirements,
                            )
                        ]
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
            time.sleep(2)
            try:
                dep = apps_v1.read_namespaced_deployment_status(deploy_name, NAMESPACE)
                ready = dep.status.ready_replicas or 0
                if ready >= replicas:
                    _report_status(remote_app, callback_url, "Ready", peer=peer)
                    return
            except client.ApiException as e:
                log.warning("Error checking deployment status: %s", e.reason)
        _report_status(remote_app, callback_url, "Timeout", peer=peer)

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
