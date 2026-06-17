from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from tests.core.grpc_client import GRPCClient
from tests.core.k8s_client import K8sClient
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until, run_unchecked


def _node_is_ready(kubeconfig: Path) -> bool:
    output, rc = run_unchecked(
        "kubectl", "--kubeconfig", str(kubeconfig),
        "--insecure-skip-tls-verify", "get", "nodes", "-o", "json",
    )
    if rc != 0:
        return False
    nodes: list[dict[str, Any]] = json.loads(output)["items"]
    return any(
        any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"])
        for n in nodes
    )


def _cos_available(kubeconfig: Path) -> tuple[bool, str]:
    output, rc = run_unchecked(
        "kubectl", "--kubeconfig", str(kubeconfig),
        "--insecure-skip-tls-verify", "get", "clusteroperators", "-o", "json",
    )
    if rc != 0:
        return False, "kubectl failed"
    operators: list[dict[str, Any]] = json.loads(output)["items"]
    if not operators:
        return False, "no operators"
    for op in operators:
        conditions = op.get("status", {}).get("conditions", [])
        if not conditions:
            continue
        available = any(c["type"] == "Available" and c["status"] == "True" for c in conditions)
        degraded = any(c["type"] == "Degraded" and c["status"] == "True" for c in conditions)
        if not available or degraded:
            return False, op["metadata"]["name"]
    return True, ""


def test_cluster_order_lifecycle(
    ready_cluster: tuple[str, str], grpc: GRPCClient, k8s_hub_client: K8sClient, cli: OsacCLI
) -> None:
    uuid, co_name = ready_cluster

    assert uuid in grpc.list_cluster_ids()

    hc_name: str = k8s_hub_client.get_cluster_order_hosted_cluster_name(name=co_name)
    assert hc_name, f"No HostedCluster name in ClusterOrder {co_name}"

    hc_ns: str = k8s_hub_client.get_cluster_order_namespace(name=co_name)
    assert hc_ns, f"No namespace in ClusterOrder {co_name}"

    kubeconfig_yaml: str = cli.get_cluster_credential("kubeconfig", uuid=uuid)
    kubeconfig: dict[str, Any] = yaml.safe_load(kubeconfig_yaml)
    assert kubeconfig["clusters"][0]["cluster"]["server"].startswith("https://")

    fd, tmp = tempfile.mkstemp(suffix=".kubeconfig")
    os.close(fd)
    kc_path = Path(tmp)
    kc_path.write_text(kubeconfig_yaml)
    try:
        poll_until(
            fn=lambda: _node_is_ready(kc_path),
            until=lambda ready: ready,
            retries=80,
            delay=15,
            description="worker node Ready in hosted cluster",
        )

        poll_until(
            fn=lambda: _cos_available(kc_path),
            until=lambda result: result[0],
            retries=40,
            delay=15,
            description="all ClusterOperators Available",
        )
    finally:
        kc_path.unlink(missing_ok=True)
