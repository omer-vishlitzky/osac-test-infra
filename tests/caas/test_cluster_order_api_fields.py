from __future__ import annotations

import json
from typing import Any

from tests.core.k8s_client import K8sClient


def test_cluster_order_api_fields(
    ready_cluster: tuple[str, str], cluster_template: str, k8s_hub_client: K8sClient
) -> None:
    _, co_name = ready_cluster

    co: dict[str, Any] = k8s_hub_client.get_json(resource="clusterorder", name=co_name)
    spec: dict[str, Any] = co["spec"]
    assert spec["templateID"] == cluster_template, f"templateID mismatch: {spec['templateID']} != {cluster_template}"

    cr_params: dict[str, str] = json.loads(spec["templateParameters"])
    assert len(cr_params["pull_secret"]) > 10, "pull_secret missing or empty"
    assert cr_params["ssh_public_key"], "ssh_public_key missing"

    assert spec["nodeRequests"][0]["resourceClass"] == "ci-worker", "resourceClass mismatch"
    assert spec["nodeRequests"][0]["numberOfNodes"] == 1, "numberOfNodes mismatch"

    status: dict[str, Any] = co["status"]
    assert status["phase"] == "Ready", f"Expected Ready phase, got {status['phase']}"

    cluster_ref: dict[str, str] = status["clusterReference"]
    assert cluster_ref["hostedClusterName"], "Missing hostedClusterName"
    assert cluster_ref["namespace"], "Missing namespace"
    assert cluster_ref["serviceAccountName"], "Missing serviceAccountName"
    assert cluster_ref["roleBindingName"], "Missing roleBindingName"
