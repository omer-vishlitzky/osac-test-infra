"""Structural tests for the CI Observe run-investigation dashboard.

monitoring/config/grafana/dashboards/ci-observe-runs.json is provisioned by
monitoring/config/grafana/dashboards.yml into the OSAC CI folder next to the
other dashboards. It queries the ci-observe capture backend through three
datasources (Tempo, Loki, Prometheus), so these tests pin the datasource
uids, the capture_id template variable and the TraceQL/LogQL/PromQL shapes
that the panels depend on. The queries themselves were validated against a
live grafana/otel-lgtm stack with replayed captures; see the ci-observe plan
for the recorded evidence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-runs.json"

# Datasource uids owned by the ci-observe capture backend. They are created
# outside this dashboard (datasource provisioning), never inside it.
CI_OBSERVE_DATASOURCE_UIDS = {"ci-observe-traces", "ci-observe-logs", "ci-observe-metrics"}

# Grafana built-in variables ($__interval, $__rate_interval, ...) are allowed
# in queries; every other $name must be a dashboard template variable.
GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text())


def _existing_datasource_uids() -> set[str]:
    """Datasource uids used by the other provisioned OSAC CI dashboards."""
    uids: set[str] = set()
    for path in sorted(DASHBOARDS_DIR.glob("*.json")):
        if path == DASHBOARD_FILE:
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        for panel in data.get("panels", []):
            ds = panel.get("datasource")
            if isinstance(ds, dict) and ds.get("uid"):
                uids.add(ds["uid"])
    return uids


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _iter_panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def test_json_is_valid_and_parses(dashboard: dict) -> None:
    assert DASHBOARD_FILE.exists()
    assert isinstance(dashboard, dict)


def test_required_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-runs"
    assert dashboard["title"] == "CI Observe — Run Investigation"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1


def test_all_three_datasources_are_referenced(dashboard: dict) -> None:
    raw = DASHBOARD_FILE.read_text()
    for uid in CI_OBSERVE_DATASOURCE_UIDS:
        assert uid in raw, f"datasource uid {uid} is not referenced anywhere"


def test_capture_id_variable_exists(dashboard: dict) -> None:
    variables = dashboard["templating"]["list"]
    names = {v["name"] for v in variables}
    assert "capture_id" in names

    capture_var = next(v for v in variables if v["name"] == "capture_id")
    assert capture_var["type"] == "query"
    assert capture_var["datasource"]["uid"] == "ci-observe-logs"
    query = capture_var["query"]
    assert query["label"] == "osac_ci_capture_id"
    assert query["type"] == 1  # Loki label values


def test_every_panel_datasource_is_known(dashboard: dict) -> None:
    allowed = CI_OBSERVE_DATASOURCE_UIDS | _existing_datasource_uids()
    panels = [p for p in _iter_panels(dashboard["panels"]) if p.get("type") != "row"]
    assert panels, "dashboard has no content panels"
    for panel in panels:
        for ds in [panel.get("datasource")] + [t.get("datasource") for t in panel.get("targets", [])]:
            if ds is None:
                continue
            assert isinstance(ds, dict), f"panel {panel['title']}: datasource must be an object"
            assert ds["uid"] in allowed, f"panel {panel['title']}: unknown datasource uid {ds['uid']}"


def test_rows_present(dashboard: dict) -> None:
    titles = [p["title"] for p in dashboard["panels"] if p["type"] == "row"]
    assert titles == ["Run overview", "Tests", "Service requests", "Logs", "Cluster (kubelet metrics)"]


def test_tempo_targets_use_traceql_search(dashboard: dict) -> None:
    tempo_targets = [
        (panel, target)
        for panel in _iter_panels(dashboard["panels"])
        for target in panel.get("targets", [])
        if panel.get("datasource", {}).get("uid") == "ci-observe-traces"
    ]
    assert tempo_targets, "dashboard has no Tempo targets"
    for panel, target in tempo_targets:
        assert target.get("queryType") == "traceql", f"{panel['title']}: unexpected Tempo queryType"
        assert 'resource."osac.ci.capture_id" = "$capture_id"' in target["query"], (
            f"{panel['title']}: TraceQL query is not filtered by capture id"
        )


def test_loki_and_prometheus_targets_filter_by_capture_or_use_real_metrics(dashboard: dict) -> None:
    loki_exprs = [
        t["expr"]
        for p in _iter_panels(dashboard["panels"])
        for t in p.get("targets", [])
        if p.get("datasource", {}).get("uid") == "ci-observe-logs"
    ]
    assert loki_exprs, "dashboard has no Loki targets"
    for expr in loki_exprs:
        assert 'osac_ci_capture_id="$capture_id"' in expr, f"Loki query not scoped to capture: {expr}"

    prom_exprs = [
        t["expr"]
        for p in _iter_panels(dashboard["panels"])
        for t in p.get("targets", [])
        if p.get("datasource", {}).get("uid") == "ci-observe-metrics"
    ]
    assert prom_exprs, "dashboard has no Prometheus targets"
    # Real kubelet_stats metric names as Prometheus renames them on OTLP
    # ingestion (dots to underscores, units suffixes). If these change the
    # Cluster row silently goes empty, so pin them.
    expected_metrics = {
        "k8s_node_cpu_time_seconds_total",
        "k8s_node_memory_working_set_bytes",
        "k8s_pod_cpu_time_seconds_total",
        "k8s_pod_memory_working_set_bytes",
    }
    used = {m for expr in prom_exprs for m in expected_metrics if m in expr}
    assert used == expected_metrics, f"missing kubelet metrics in Cluster row: {expected_metrics - used}"


def test_default_time_range_is_seven_days(dashboard: dict) -> None:
    assert dashboard["time"] == {"from": "now-7d", "to": "now"}


def test_no_leftover_template_tokens(dashboard: dict) -> None:
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
    for text in _walk_strings(dashboard):
        for match in token.findall(text):
            if match.startswith("$"):
                continue
            assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                f"unresolved template token {match!r} in dashboard strings"
            )
