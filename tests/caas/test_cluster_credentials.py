from __future__ import annotations

import yaml

from tests.core.osac_cli import OsacCLI


def test_get_kubeconfig(ready_cluster: tuple[str, str], cli: OsacCLI) -> None:
    uuid, _ = ready_cluster
    output: str = cli.get_cluster_credential("kubeconfig", uuid=uuid)
    assert output, "kubeconfig output should not be empty"
    kubeconfig = yaml.safe_load(output)
    assert "clusters" in kubeconfig, "kubeconfig should have clusters key"
    assert len(kubeconfig["clusters"]) > 0, "kubeconfig should have at least one cluster"
    server: str = kubeconfig["clusters"][0]["cluster"]["server"]
    assert server.startswith("https://"), f"server URL should start with https://, got: {server}"


def test_get_password(ready_cluster: tuple[str, str], cli: OsacCLI) -> None:
    uuid, _ = ready_cluster
    output: str = cli.get_cluster_credential("password", uuid=uuid)
    assert output, "password output should not be empty"
    assert len(output.strip()) > 0, "password should be a non-empty string"
