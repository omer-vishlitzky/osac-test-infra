from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

import pytest

from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cluster_deletion,
    wait_for_cluster_grpc_removal,
    wait_for_cluster_order_cr,
    wait_for_cluster_ready,
)
from tests.core.k8s_client import K8sClient
from tests.core.osac_cli import OsacCLI
from tests.core.runner import env


@pytest.fixture(scope="session")
def cluster_template() -> str:
    return env("OSAC_CLUSTER_TEMPLATE", "osac.templates.ocp_ci_small")


@pytest.fixture(scope="session")
def pull_secret_path() -> str:
    return env("OSAC_PULL_SECRET_PATH")


@pytest.fixture(scope="session")
def ssh_public_key_path() -> str:
    return env("OSAC_SSH_PUBLIC_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa.pub"))


@pytest.fixture(scope="session")
def ready_cluster(
    cli: OsacCLI, k8s_hub_client: K8sClient, grpc: GRPCClient,
    cluster_template: str, pull_secret_path: str, ssh_public_key_path: str,
) -> Generator[tuple[str, str], None, None]:
    """Session-scoped: provisions ONE cluster, shared by all tests that need it.
    Teardown deletes the cluster and verifies cleanup — teardown errors fail the suite."""
    uuid: str = cli.create_cluster(
        template=cluster_template,
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )
    co_name: str = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)
    try:
        wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)
    except Exception:
        if k8s_hub_client.is_present(resource="clusterorder", name=co_name):
            cli.delete_cluster(uuid=uuid)
            wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
            wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
        raise

    yield uuid, co_name

    # Teardown: delete and verify — errors here fail the suite
    cli.delete_cluster(uuid=uuid)
    wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
    wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
