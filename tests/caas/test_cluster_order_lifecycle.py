from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cluster_deletion,
    wait_for_cluster_grpc_removal,
    wait_for_cluster_order_cr,
    wait_for_cluster_ready,
)
from tests.core.k8s_client import K8sClient
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until, run, run_unchecked


def _inner_kubectl(kubeconfig: Path, *args: str) -> str:
    return run(
        "kubectl", "--kubeconfig", str(kubeconfig),
        "--insecure-skip-tls-verify", *args,
    )


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


@pytest.fixture
def cluster_order(
    cli: OsacCLI, k8s_hub_client: K8sClient, cluster_template: str, pull_secret_path: str, ssh_public_key_path: str
):
    uuid: str = cli.create_cluster(
        template=cluster_template,
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )
    co_name: str = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)
    yield uuid, co_name
    if k8s_hub_client.is_present(resource="clusterorder", name=co_name):
        cli.delete_cluster(uuid=uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)


def test_cluster_order_lifecycle(
    cluster_order: tuple[str, str], grpc: GRPCClient, k8s_hub_client: K8sClient, cli: OsacCLI
) -> None:
    uuid, co_name = cluster_order

    assert uuid in grpc.list_cluster_ids()

    wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)

    hc_name: str = k8s_hub_client.get_cluster_order_hosted_cluster_name(name=co_name)
    assert hc_name, f"No HostedCluster name in ClusterOrder {co_name}"

    hc_ns: str = k8s_hub_client.get_cluster_order_namespace(name=co_name)
    assert hc_ns, f"No namespace in ClusterOrder {co_name}"

    kubeconfig_yaml: str = cli.get_cluster_credential("kubeconfig", uuid=uuid)
    kubeconfig: dict[str, Any] = yaml.safe_load(kubeconfig_yaml)
    assert kubeconfig["clusters"][0]["cluster"]["server"].startswith("https://")

    kc_path = Path(tempfile.mktemp(suffix=".kubeconfig"))
    kc_path.write_text(kubeconfig_yaml)
    try:
        poll_until(
            fn=lambda: _node_is_ready(kc_path),
            until=lambda ready: ready,
            retries=60,
            delay=15,
            description="worker node Ready in hosted cluster",
        )

        operators: list[dict[str, Any]] = json.loads(
            _inner_kubectl(kc_path, "get", "clusteroperators", "-o", "json")
        )["items"]
        assert len(operators) > 0, "No ClusterOperators found"
        for op in operators:
            op_name: str = op["metadata"]["name"]
            conditions: list[dict[str, str]] = op["status"]["conditions"]
            available = any(c["type"] == "Available" and c["status"] == "True" for c in conditions)
            degraded = any(c["type"] == "Degraded" and c["status"] == "True" for c in conditions)
            assert available, f"ClusterOperator {op_name} is not Available"
            assert not degraded, f"ClusterOperator {op_name} is Degraded"
    finally:
        kc_path.unlink(missing_ok=True)

    cli.delete_cluster(uuid=uuid)
    wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
    wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
