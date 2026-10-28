"""Check the read-only pilot through an actual Grafana instance.

The pilot under test is a real running Grafana (loopback only, anonymous
Viewer) provisioned from a local dashboard file; the test compares what
Grafana serves against the CURRENT local dashboard candidate, so a stale
served snapshot fails. Grafana's file provider re-reads the dashboard
directory roughly every updateIntervalSeconds (5 s), so content checks
wait a bounded time for the provider to pick up a refreshed file instead
of trusting a cached title.

These tests never fetch GitHub, never write the shared dashboard, and
never need a browser: they use Grafana's read APIs and the anonymous
Viewer role only. They are readiness and negative-permission checks for
the parent's integration run, not the full launcher test.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

PROVIDER_RELOAD_TIMEOUT_SECONDS = 40
PROVIDER_POLL_SECONDS = 2


@dataclass(frozen=True, kw_only=True)
class Pilot:
    url: str
    dashboard: dict[str, object]
    snapshot: dict[str, object]


@pytest.fixture(scope="module")
def pilot() -> Pilot:
    names = (
        "CI_OBSERVABILITY_PILOT_GRAFANA_URL",
        "CI_OBSERVABILITY_PILOT_DASHBOARD",
        "CI_OBSERVABILITY_PILOT_SNAPSHOT",
    )
    values = [os.environ.get(name) for name in names]
    if not any(values):
        pytest.skip("local Grafana pilot is not configured")
    assert all(values), "all three pilot environment settings are required"
    url = str(values[0]).rstrip("/")
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", url), "pilot tests require loopback HTTP"
    dashboard = json.loads(Path(str(values[1])).read_text())
    snapshot = json.loads(Path(str(values[2])).read_text())
    return Pilot(url=url, dashboard=dashboard, snapshot=snapshot)


def request_json(*, url: str, data: dict[str, object] | None = None) -> dict[str, object]:
    encoded = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json", "Origin": url.split("/api/")[0]},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(32 * 1024 * 1024 + 1)
    assert len(body) <= 32 * 1024 * 1024, "Grafana response exceeded the test bound"
    result = json.loads(body)
    assert isinstance(result, dict)
    return result


def inline_data_signature(*, dashboard: dict[str, object]) -> list[tuple[str, str, str]]:
    """Include text so a later snapshot of an unchanged run is still detected."""

    signature: list[tuple[str, str, str]] = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            signature.append((panel["title"], target["refId"], target["data"]))
        if panel["type"] == "text":
            signature.append((panel["title"], "text", panel["options"]["content"]))
    return signature


def fetch_served_dashboard(*, pilot: Pilot) -> dict[str, object]:
    result = request_json(url=f"{pilot.url}/api/dashboards/uid/ci-run-pilot")
    served = result["dashboard"]
    assert isinstance(served, dict)
    return served


def test_pilot_dashboard_is_served_and_current(*, pilot: Pilot) -> None:
    """The served dashboard matches the CURRENT local candidate.

    Waits a bounded time for Grafana's file provider to reload a
    refreshed dashboard file; a stale served snapshot (the provider has
    not picked up the new file, or the local candidate was re-rendered
    from a newer snapshot than what Grafana serves) fails here instead
    of passing on a cached title.
    """

    deadline = time.monotonic() + PROVIDER_RELOAD_TIMEOUT_SECONDS
    last_signature: list[tuple[str, str, str]] | None = None
    served: dict[str, object] | None = None
    while True:
        try:
            served = fetch_served_dashboard(pilot=pilot)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        if served is not None:
            last_signature = inline_data_signature(dashboard=served)
            if last_signature == inline_data_signature(dashboard=pilot.dashboard):
                break
        if time.monotonic() >= deadline:
            assert served is not None, "Grafana did not provision the pilot dashboard"
            assert served["uid"] == "ci-run-pilot"
            assert served["editable"] is False
            pytest.fail(
                "served pilot dashboard is stale: its inline data does not match the current local dashboard candidate"
            )
        time.sleep(PROVIDER_POLL_SECONDS)
    assert served is not None
    assert served["uid"] == "ci-run-pilot"
    assert served["title"] == pilot.dashboard["title"]
    assert served["editable"] is False
    assert last_signature is not None and len(last_signature) > 0


def test_signature_detects_a_new_as_of_time_without_table_changes() -> None:
    earlier = {
        "panels": [
            {"title": "Run identity", "type": "text", "options": {"content": "Snapshot taken at 08:00Z"}},
            {"title": "Jobs", "type": "table", "targets": [{"refId": "A", "data": '[{"id": 1}]'}]},
        ]
    }
    later = json.loads(json.dumps(earlier))
    assert inline_data_signature(dashboard=earlier) == inline_data_signature(dashboard=later)
    later["panels"][0]["options"]["content"] = "Snapshot taken at 09:00Z"
    assert inline_data_signature(dashboard=earlier) != inline_data_signature(dashboard=later)


def test_pilot_queries_match_their_snapshot_rows(*, pilot: Pilot) -> None:
    expected_counts = {
        "Jobs": len(pilot.snapshot["jobs"]),
        "Steps (all jobs)": sum(len(job["steps"]) for job in pilot.snapshot["jobs"]),
        "Artifacts and evidence": len(pilot.snapshot["artifacts"]),
    }
    query_count = 0
    for panel in pilot.dashboard["panels"]:
        for target in panel.get("targets", []):
            assert target["source"] == "inline", "pilot must not request arbitrary URLs"
            assert target.get("url", "") == ""
            assert target["parser"] == "backend"
            rows = json.loads(target["data"])
            assert isinstance(rows, list)
            assert len(rows) == expected_counts[panel["title"]]
            query = dict(target)
            query["datasource"] = {"type": "yesoreyeram-infinity-datasource", "uid": "ci-pilot-inline"}
            result = request_json(url=f"{pilot.url}/api/ds/query", data={"from": "0", "to": "1", "queries": [query]})[
                "results"
            ][query["refId"]]
            assert result["status"] == 200, result.get("error")
            assert "error" not in result
            frames = result["frames"]
            actual_rows = 0
            for frame in frames:
                columns = frame["data"]["values"]
                lengths = {len(column) for column in columns}
                assert len(lengths) <= 1
                actual_rows += next(iter(lengths), 0)
            assert actual_rows == len(rows), panel["title"]
            query_count += 1
    # A valid run with no artifacts renders two table queries (jobs and
    # steps); the artifacts panel adds a third only when artifacts exist.
    # The per-target row-count assertions above are the real data proof.
    assert query_count >= 2, "pilot needs the jobs and steps data panels"


def test_pilot_exposes_only_a_viewer(*, pilot: Pilot) -> None:
    payload = {"dashboard": {"uid": "ci-pilot-write-probe", "title": "Write must be denied", "panels": []}}
    with pytest.raises(urllib.error.HTTPError) as error:
        request_json(url=f"{pilot.url}/api/dashboards/db", data=payload)
    assert error.value.code in (401, 403), "anonymous pilot access must be read-only"


def test_pilot_data_source_refuses_url_queries(*, pilot: Pilot) -> None:
    query = {
        "refId": "URL",
        "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "ci-pilot-inline"},
        "type": "json",
        "source": "url",
        "format": "table",
        "parser": "backend",
        "url": f"{pilot.url}/api/health",
        "columns": [{"selector": "version", "text": "Version", "type": "string"}],
        "root_selector": "",
    }
    try:
        result = request_json(url=f"{pilot.url}/api/ds/query", data={"from": "0", "to": "1", "queries": [query]})
    except urllib.error.HTTPError as error:
        assert error.code == 400
        result = json.load(error)
    denied = result["results"]["URL"]
    assert denied["status"] == 500
    assert "requested URL not allowed" in denied["error"]
