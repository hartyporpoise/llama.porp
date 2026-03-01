"""
Marshall model types (dataclasses) to OpenAPI 3 schema dicts.
Single source of truth: schemas are derived from porpulsion.models, not duplicated.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal, get_args, get_origin

from porpulsion import models


def _type_to_schema(typ: Any, refs: dict[type, str]) -> dict[str, Any]:
    """Map a Python type to an OpenAPI schema dict. refs maps dataclass -> component name for $ref."""
    if typ is type(None):
        return {"type": "string", "nullable": True}
    origin = get_origin(typ)
    args = get_args(typ)

    # Optional / X | None
    if args and type(None) in args:
        inner = next(a for a in args if a is not type(None))
        s = _type_to_schema(inner, refs)
        s = dict(s)
        s["nullable"] = True
        return s

    # Literal
    if origin is Literal and args and all(isinstance(a, str) for a in args):
        return {"type": "string", "enum": list(args)}

    # list[T]
    if origin is list:
        item_type = args[0] if args else Any
        return {"type": "array", "items": _type_to_schema(item_type, refs)}

    # dict
    if origin is dict:
        return {"type": "object", "additionalProperties": True}

    # dataclass -> $ref
    if dataclasses.is_dataclass(typ) and typ in refs:
        return {"$ref": f"#/components/schemas/{refs[typ]}"}

    # primitives
    if typ is str or typ == str:
        return {"type": "string"}
    if typ is int or typ == int:
        return {"type": "integer"}
    if typ is bool or typ == bool:
        return {"type": "boolean"}
    if typ is float or typ == float:
        return {"type": "number"}

    return {"type": "object"}


def _dataclass_to_schema(cls: type, refs: dict[type, str]) -> dict[str, Any]:
    """Build OpenAPI schema for a dataclass from its fields and type hints."""
    hints = {}
    try:
        hints = __import__("typing").get_type_hints(cls)
    except Exception:
        pass
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        name = f.name
        if name.startswith("_"):
            continue
        typ = hints.get(name, f.type)
        schema = _type_to_schema(typ, refs)
        if name == "ca_pem":
            continue
        properties[name] = schema
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(name)
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    if cls.__doc__:
        doc = cls.__doc__.strip().split("\n")[0]
        if doc:
            out["description"] = doc
    return out


# Dependencies first. API name may differ from class name.
MODEL_ORDER: list[tuple[type, str]] = [
    (models.EnvVarSource, "EnvVarSource"),
    (models.EnvVar, "EnvVar"),
    (models.PortSpec, "PortSpec"),
    (models.ResourceRequirements, "ResourceRequirements"),
    (models.ReadinessProbe, "ReadinessProbe"),
    (models.SecurityContext, "SecurityContext"),
    (models.RemoteAppSpec, "RemoteAppSpec"),
    (models.Peer, "Peer"),
    (models.RemoteApp, "RemoteApp"),
    (models.AgentSettings, "Settings"),
]
REF_MAP: dict[type, str] = {cls: name for cls, name in MODEL_ORDER}

# RemoteApp Spec: documentation and examples (aligned with dashboard Docs tab)
REMOTE_APP_SPEC_DESCRIPTION = (
    "The spec is submitted as YAML in the Deploy form. All fields except **image** are optional. "
    "Subject to peer quota and image policy."
)
REMOTE_APP_SPEC_PROPERTY_DESCRIPTIONS: dict[str, str] = {
    "image": "Container image reference, e.g. nginx:latest or ghcr.io/org/app:v1.2",
    "replicas": "Number of pod replicas. Subject to peer quota limits.",
    "port": "Single container port (legacy). Prefer ports.",
    "ports": "Ports to expose. Each entry: port (required), name (optional).",
    "resources": "Kubernetes resource requests and limits. requests/limits with cpu (e.g. 250m, 1) and memory (e.g. 128Mi, 1Gi). Checked against peer quotas.",
    "command": "Override the container ENTRYPOINT, e.g. [\"/bin/sh\", \"-c\"].",
    "args": "Override the container CMD / arguments.",
    "env": "Environment variables. Each entry: name + value, or valueFrom.secretKeyRef / valueFrom.configMapKeyRef.",
    "imagePullPolicy": "Always | IfNotPresent | Never. Use Always with mutable tags like latest.",
    "imagePullSecrets": "Names of k8s Secrets containing registry credentials.",
    "readinessProbe": "Probe for when to send traffic. httpGet (path, port) or exec (command), plus initialDelaySeconds, periodSeconds, failureThreshold.",
    "securityContext": "Pod/container security: runAsNonRoot, runAsUser, runAsGroup, fsGroup, readOnlyRootFilesystem.",
}

REMOTE_APP_SPEC_EXAMPLES = [
    {
        "summary": "Minimal — nginx serving on port 80",
        "value": {
            "image": "nginx:latest",
            "replicas": 1,
            "ports": [{"port": 80, "name": "http"}],
        },
    },
    {
        "summary": "With resources and env vars",
        "value": {
            "image": "myapp:v2.1",
            "replicas": 2,
            "resources": {
                "requests": {"cpu": "250m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
            "ports": [{"port": 8080, "name": "http"}, {"port": 9090, "name": "metrics"}],
            "env": [
                {"name": "NODE_ENV", "value": "production"},
                {"name": "API_KEY", "valueFrom": {"secretKeyRef": {"name": "my-secret", "key": "api-key"}}},
            ],
        },
    },
    {
        "summary": "Custom entrypoint",
        "value": {
            "image": "python:3.11-slim",
            "command": ["/bin/sh", "-c"],
            "args": ["python -m http.server 8000"],
            "ports": [{"port": 8000, "name": "http"}],
        },
    },
    {
        "summary": "Readiness probe + security hardening",
        "value": {
            "image": "ghcr.io/myorg/api:v3.0",
            "replicas": 2,
            "ports": [{"port": 8080, "name": "http"}],
            "imagePullSecrets": ["ghcr-credentials"],
            "readinessProbe": {
                "httpGet": {"path": "/healthz", "port": 8080},
                "initialDelaySeconds": 10,
                "periodSeconds": 5,
            },
            "securityContext": {"runAsNonRoot": True, "readOnlyRootFilesystem": True},
        },
    },
]


def schemas_from_models() -> dict[str, dict[str, Any]]:
    """Return OpenAPI components/schemas dict keyed by schema name, derived from models."""
    out: dict[str, dict[str, Any]] = {}
    for cls, name in MODEL_ORDER:
        out[name] = _dataclass_to_schema(cls, REF_MAP)
    # Enrich RemoteAppSpec with docs and examples (match dashboard Docs tab)
    if "RemoteAppSpec" in out:
        s = out["RemoteAppSpec"]
        s["description"] = REMOTE_APP_SPEC_DESCRIPTION
        for prop, desc in REMOTE_APP_SPEC_PROPERTY_DESCRIPTIONS.items():
            if "properties" in s and prop in s["properties"]:
                s["properties"][prop]["description"] = desc
        s["example"] = REMOTE_APP_SPEC_EXAMPLES[0]["value"]
    return out


def remote_app_request_examples() -> dict[str, Any]:
    """POST /remoteapp request body examples: same as RemoteApp Spec examples, with name added."""
    return {
        "minimal": {
            "summary": REMOTE_APP_SPEC_EXAMPLES[0]["summary"],
            "value": {"name": "my-nginx", "spec": REMOTE_APP_SPEC_EXAMPLES[0]["value"]},
        },
        "with_resources": {
            "summary": REMOTE_APP_SPEC_EXAMPLES[1]["summary"],
            "value": {"name": "myapp", "spec": REMOTE_APP_SPEC_EXAMPLES[1]["value"]},
        },
        "custom_entrypoint": {
            "summary": REMOTE_APP_SPEC_EXAMPLES[2]["summary"],
            "value": {"name": "py-server", "spec": REMOTE_APP_SPEC_EXAMPLES[2]["value"]},
        },
        "readiness_and_security": {
            "summary": REMOTE_APP_SPEC_EXAMPLES[3]["summary"],
            "value": {"name": "api", "spec": REMOTE_APP_SPEC_EXAMPLES[3]["value"]},
        },
    }


def status_schema() -> dict[str, Any]:
    """GET /status response."""
    return {
        "type": "object",
        "properties": {
            "agent": {"type": "string"},
            "peers": {"type": "array", "items": {"$ref": "#/components/schemas/PeerEntry"}},
            "local_apps": {"type": "integer"},
            "remote_apps": {"type": "integer"},
        },
    }


def peer_entry_schema() -> dict[str, Any]:
    """GET /peers list item."""
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "url": {"type": "string"},
            "channel": {"type": "string", "enum": ["connected", "disconnected"]},
            "connected_at": {"type": "string"},
            "status": {"type": "string"},
            "attempts": {"type": "integer"},
            "error": {"type": "string"},
        },
    }
