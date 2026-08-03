from __future__ import annotations

import pytest

from tests.catalog.conftest import unique_name
from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cr,
    wait_for_deletion,
    wait_for_grpc_removal,
    wait_for_provision,
    wait_for_running,
)
from tests.core.k8s_client import K8sClient
from tests.core.metering import MeteringCollector
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until, run_unchecked


def _wait_for_metering_ready(namespace: str) -> None:
    """Wait for the metering pod to become ready after restart."""
    poll_until(
        fn=lambda: run_unchecked(
            "oc", "get", "pods", "-l", "app.kubernetes.io/component=metering",
            "-n", namespace,
            "-o", "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}",
        )[0].strip(),
        until=lambda v: v == "True",
        retries=60,
        delay=5,
        description="metering pod ready after restart",
    )


@pytest.mark.metering
def test_metering_reconciliation_after_restart(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    k8s_virt_client: K8sClient,
    vm_template: str,
    default_subnet: str,
    namespace: str,
    metering: MeteringCollector,
) -> None:
    """Verify metering recovers after pod restart (startup reconciliation).

    Creates a billable VM, restarts the metering pod, then verifies
    heartbeats resume — confirming startup reconciliation rebuilt the
    State Projection and the readiness gate worked correctly.
    """
    name = unique_name("e2e-ci")
    uuid: str = cli.create_compute_instance(
        name=name,
        template=vm_template,
        network_attachments=[{"subnet": default_subnet}],
    )

    ci_name: str = wait_for_cr(k8s=k8s_hub_client, uuid=uuid)
    try:
        wait_for_provision(k8s=k8s_hub_client, name=ci_name)
        wait_for_running(k8s=k8s_hub_client, name=ci_name)

        metering.expect("osac.resource.created.v1", resource_id=uuid)
        metering.expect("osac.resource.started.v1", resource_id=uuid)
        metering.verify()

        # Restart the metering pod by deleting it (Deployment recreates it)
        run_unchecked(
            "oc", "delete", "pods", "-l", "app.kubernetes.io/component=metering",
            "-n", namespace, "--wait=false",
        )

        _wait_for_metering_ready(namespace)

        # After restart, startup reconciliation should rebuild the State
        # Projection from fulfillment List API. Heartbeats should resume
        # for the still-running VM.
        metering.expect("osac.resource.heartbeat.v1", resource_id=uuid, timeout=180)
        metering.verify()
    finally:
        cli.delete_compute_instance(uuid=uuid)
        wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
        wait_for_grpc_removal(grpc=grpc, uuid=uuid)
