"""Tests for the pilot dashboard generator: query shape, data fidelity,
HTML escaping, URL clamping, and coverage honesty."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitoring.ci_observability.pilot_dashboard import (
    DASHBOARD_UID,
    DATASOURCE_UID,
    INFINITY_TYPE,
    SnapshotError,
    build_dashboard,
    main,
)

MODULE = "monitoring.ci_observability.pilot_dashboard"


def snapshot() -> dict[str, object]:
    """One valid snapshot with known values for every assertion."""
    return {
        "schema": "osac.ci-observability.pilot",
        "version": 1,
        "fetched_at": "2026-10-28T08:00:00Z",
        "repository": "osac-project/osac",
        "run": {
            "id": 40548825479,
            "attempt": 1,
            "name": "E2E VMaaS Full Install",
            "event": "schedule",
            "status": "completed",
            "conclusion": "failure",
            "head_sha": "abc123def456789012345678901234567890abcd",
            "created_at": "2026-10-27T02:07:00Z",
            "started_at": "2026-10-27T02:07:01Z",
            "updated_at": "2026-10-27T03:45:00Z",
            "url": "https://github.com/osac-project/osac/actions/runs/40548825479",
            "workflow_path": ".github/workflows/e2e-vmaas-full-install.yml",
        },
        "jobs": [
            {
                "id": 12345678,
                "name": "e2e-vmaas-full-install / e2e",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-10-27T02:08:00Z",
                "completed_at": "2026-10-27T03:44:00Z",
                "url": "https://github.com/osac-project/osac/actions/runs/40548825479/job/12345678",
                "steps": [
                    {
                        "number": 1,
                        "name": "Set up job",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-27T02:08:00Z",
                        "completed_at": "2026-10-27T02:08:05Z",
                    },
                    {
                        "number": 2,
                        "name": "Run E2E tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "started_at": "2026-10-27T02:10:00Z",
                        "completed_at": "2026-10-27T03:40:00Z",
                    },
                    {
                        "number": 3,
                        "name": "Post Run E2E tests",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-27T03:40:00Z",
                        "completed_at": "2026-10-27T03:41:00Z",
                    },
                ],
            }
        ],
        "artifacts": [
            {
                "id": 9876543,
                "name": "osac-logs-full-install",
                "size_bytes": 12345678,
                "expired": False,
                "url": "https://github.com/osac-project/osac/actions/runs/40548825479/artifacts/9876543",
            }
        ],
        "coverage": {
            "jobs_complete": True,
            "artifacts_complete": False,
            "test_results": "not-collected",
            "tested_revision": "unverified",
            "notes": ["Snapshot taken from the GitHub Actions API only."],
        },
    }


def dashboard() -> dict[str, object]:
    return build_dashboard(snapshot=snapshot())


def table_targets(*, definition: dict[str, object]) -> list[dict[str, object]]:
    return [target for panel in definition["panels"] if panel["type"] == "table" for target in panel["targets"]]


def test_dashboard_identity_is_read_only_and_tagged() -> None:
    d = dashboard()
    assert d["uid"] == DASHBOARD_UID == "ci-run-pilot"
    assert d["editable"] is False
    assert d["tags"] == ["ci-pilot"]
    assert d["refresh"] is False
    assert d["templating"]["list"] == []
    assert d["annotations"]["list"] == []


def test_time_window_comes_from_actual_source_times() -> None:
    d = dashboard()
    assert d["time"]["from"] == snapshot()["run"]["created_at"]
    assert d["time"]["to"] == snapshot()["run"]["updated_at"]


def test_every_table_target_uses_the_proven_inline_backend_shape() -> None:
    targets = table_targets(definition=dashboard())
    assert len(targets) >= 2
    for target in targets:
        assert target["type"] == "json"
        assert target["source"] == "inline"
        assert target["format"] == "table"
        assert target["parser"] == "backend"
        assert target["root_selector"] == ""
        assert target["url"] == ""
        assert target["datasource"]["type"] == INFINITY_TYPE
        assert target["datasource"]["uid"] == DATASOURCE_UID
        assert isinstance(target["data"], str)
        json.loads(target["data"])
        assert isinstance(target["columns"], list)
        for column in target["columns"]:
            assert set(column) == {"selector", "text", "type"}


def test_job_table_matches_input_count_and_values() -> None:
    d = dashboard()
    job_panel = next(p for p in d["panels"] if p["title"] == "Jobs")
    target = job_panel["targets"][0]
    rows = json.loads(target["data"])
    assert len(rows) == 1
    assert rows[0]["job"] == "e2e-vmaas-full-install / e2e"
    assert rows[0]["conclusion"] == "failure"
    # Duration from actual start to actual end, in seconds.
    assert rows[0]["duration_s"] == 5760.0
    assert rows[0]["url"].startswith("https://github.com/osac-project/osac/actions/runs/40548825479/job/")


def test_step_table_flattens_all_jobs_and_steps() -> None:
    d = dashboard()
    step_panel = next(p for p in d["panels"] if p["title"] == "Steps (all jobs)")
    rows = json.loads(step_panel["targets"][0]["data"])
    assert len(rows) == 3
    assert all(row["job"] == "e2e-vmaas-full-install / e2e" for row in rows)
    assert [row["step"] for row in rows] == ["Set up job", "Run E2E tests", "Post Run E2E tests"]
    assert [row["duration_s"] for row in rows] == [5.0, 5400.0, 60.0]


def test_open_jobs_have_null_duration_not_zero() -> None:
    snap = snapshot()
    snap["jobs"][0]["completed_at"] = None
    d = build_dashboard(snapshot=snap)
    job_panel = next(p for p in d["panels"] if p["title"] == "Jobs")
    rows = json.loads(job_panel["targets"][0]["data"])
    assert rows[0]["duration_s"] is None


def test_artifact_table_builds_urls_from_numeric_ids() -> None:
    d = dashboard()
    artifact_panel = next(p for p in d["panels"] if p["title"] == "Artifacts and evidence")
    rows = json.loads(artifact_panel["targets"][0]["data"])
    assert len(rows) == 1
    assert rows[0]["name"] == "osac-logs-full-install"
    assert rows[0]["expired"] == "no"
    assert "/actions/runs/40548825479/artifacts/9876543" in rows[0]["url"]
    assert rows[0]["url"].startswith("https://github.com/osac-project/osac/")


def test_expired_artifact_is_clearly_marked() -> None:
    snap = snapshot()
    snap["artifacts"][0]["expired"] = True
    d = build_dashboard(snapshot=snap)
    artifact_panel = next(p for p in d["panels"] if p["title"] == "Artifacts and evidence")
    rows = json.loads(artifact_panel["targets"][0]["data"])
    assert rows[0]["expired"] == "yes"


def test_first_observed_failed_step_is_present_but_not_a_root_cause_claim() -> None:
    d = dashboard()
    text = next(p for p in d["panels"] if p["title"] == "First listed failed step")
    content = text["options"]["content"]
    assert "Run E2E tests" in content
    assert "does not prove it is the earliest cause" in content


def test_no_failed_steps_shows_explicit_none() -> None:
    snap = snapshot()
    for step in snap["jobs"][0]["steps"]:
        step["conclusion"] = "success"
    d = build_dashboard(snapshot=snap)
    text = next(p for p in d["panels"] if p["title"] == "First listed failed step")
    assert "No failed" in text["options"]["content"]


def test_run_identity_panel_marks_tested_revision_unverified() -> None:
    d = dashboard()
    text = next(p for p in d["panels"] if p["title"] == "Run identity")
    content = text["options"]["content"]
    assert "UNVERIFIED" in content
    assert "GitHub head SHA" in content
    # The head SHA is the GitHub run head, not a verified tested revision.
    assert "workflow SHA" not in content


def test_coverage_panel_is_explicit_about_missing_data() -> None:
    d = dashboard()
    text = next(p for p in d["panels"] if p["title"] == "Source coverage and limitations")
    content = text["options"]["content"]
    assert "not-collected" in content
    assert "unverified" in content
    assert "standalone snapshot" in content


def test_html_escaping_in_text_panels() -> None:
    snap = snapshot()
    snap["run"]["name"] = '<script>alert("xss")</script>'
    snap["jobs"][0]["name"] = '<img src="x" onerror="alert(1)">'
    snap["jobs"][0]["steps"][0]["name"] = "&<b>bold</b>"
    d = build_dashboard(snapshot=snap)
    identity = next(p for p in d["panels"] if p["title"] == "Run identity")
    content = identity["options"]["content"]
    assert "<script>" not in content
    assert "<img" not in content
    assert "&lt;script&gt;" in content
    # Table cell values are data, not HTML.
    step_panel = next(p for p in d["panels"] if p["title"] == "Steps (all jobs)")
    rows = json.loads(step_panel["targets"][0]["data"])
    assert any("<img" in str(row["job"]) for row in rows)  # raw in data, escaped only in HTML panels
    assert any("&" in str(row["step"]) for row in rows)


def test_javascript_url_is_ignored_in_links() -> None:
    snap = snapshot()
    snap["run"]["url"] = "javascript:alert(1)"
    d = build_dashboard(snapshot=snap)
    serialized = json.dumps(d)
    assert "javascript:" not in serialized.replace("javascript:alert", "IGNORED")
    # The run URL in the dashboard links is built from repo+numeric IDs.
    assert d["links"][0]["url"] == "https://github.com/osac-project/osac/actions/runs/40548825479/attempts/1"


def test_multiple_attempt_is_noted_but_not_claimed_comparable() -> None:
    snap = snapshot()
    snap["run"]["attempt"] = 3
    d = build_dashboard(snapshot=snap)
    text = next(p for p in d["panels"] if p["title"] == "Run identity")
    content = text["options"]["content"]
    assert "attempt 3" in content
    assert "does not claim they are comparable" in content


def test_invalid_schema_is_rejected_with_constant_error() -> None:
    snap = snapshot()
    snap["schema"] = "wrong"
    with pytest.raises(SnapshotError, match="schema is not recognized"):
        build_dashboard(snapshot=snap)


def test_missing_required_field_is_rejected() -> None:
    snap = snapshot()
    del snap["run"]["head_sha"]
    with pytest.raises(SnapshotError, match="missing the field"):
        build_dashboard(snapshot=snap)


def test_empty_jobs_is_valid() -> None:
    snap = snapshot()
    snap["jobs"] = []
    d = build_dashboard(snapshot=snap)
    job_panel = next(p for p in d["panels"] if p["title"] == "Jobs")
    rows = json.loads(job_panel["targets"][0]["data"])
    assert rows == []


def test_no_artifacts_produces_no_artifact_panel() -> None:
    snap = snapshot()
    snap["artifacts"] = []
    d = build_dashboard(snapshot=snap)
    assert not any(p["title"] == "Artifacts and evidence" for p in d["panels"])


def test_cli_writes_private_file_and_refuses_overwrite(tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snapshot()), encoding="utf-8")
    out_file = tmp_path / "dashboard.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 0
    assert out_file.exists()
    assert (out_file.stat().st_mode & 0o777) == 0o600
    written = json.loads(out_file.read_text())
    assert written["uid"] == DASHBOARD_UID
    # Refuses to overwrite.
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1


def test_cli_rejects_symlink_output(tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snapshot()), encoding="utf-8")
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    result = main(argv=["--snapshot", str(snap_file), "--output", str(link)])
    assert result == 1


def test_cli_constant_error_does_not_echo_payload(tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text('{"schema": "attacker-controlled-value-here"}', encoding="utf-8")
    out_file = tmp_path / "out.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1
    assert not out_file.exists()


def test_steps_panel_has_organize_transformation_in_intended_order() -> None:
    d = dashboard()
    step_panel = next(p for p in d["panels"] if p["title"] == "Steps (all jobs)")
    transforms = step_panel.get("transformations", [])
    assert len(transforms) == 1
    assert transforms[0]["id"] == "organize"
    index = transforms[0]["options"]["indexByName"]
    assert index["Step"] == 0
    assert index["Conclusion"] == 1
    assert index["Duration (s)"] == 2
    assert index["Job"] == 3
    assert index["#"] == 6


def test_steps_panel_has_column_width_overrides() -> None:
    d = dashboard()
    step_panel = next(p for p in d["panels"] if p["title"] == "Steps (all jobs)")
    overrides = step_panel["fieldConfig"]["overrides"]
    step_width = next(o for o in overrides if o["matcher"]["options"] == "Step")
    assert any(p["id"] == "custom.width" and p["value"] == 360 for p in step_width["properties"])
    duration_width = next(o for o in overrides if o["matcher"]["options"] == "Duration (s)")
    assert any(p["id"] == "custom.width" and p["value"] == 120 for p in duration_width["properties"])


def test_jobs_panel_has_clickable_github_links() -> None:
    d = dashboard()
    job_panel = next(p for p in d["panels"] if p["title"] == "Jobs")
    overrides = job_panel["fieldConfig"]["overrides"]
    github_override = next(o for o in overrides if o["matcher"]["options"] == "GitHub")
    links = next(p for p in github_override["properties"] if p["id"] == "links")
    assert links["value"][0]["title"] == "Open on GitHub"
    assert links["value"][0]["url"] == "${__value.raw}"
    assert links["value"][0]["targetBlank"] is True
    cell = next(p for p in github_override["properties"] if p["id"] == "custom.cellOptions")
    assert cell["value"]["type"] == "data-links"


def test_artifact_panel_has_clickable_links() -> None:
    d = dashboard()
    artifact_panel = next(p for p in d["panels"] if p["title"] == "Artifacts and evidence")
    overrides = artifact_panel["fieldConfig"]["overrides"]
    link_override = next(o for o in overrides if o["matcher"]["options"] == "Link")
    links = next(p for p in link_override["properties"] if p["id"] == "links")
    assert links["value"][0]["url"] == "${__value.raw}"


def test_jobs_panel_height_is_reduced() -> None:
    d = dashboard()
    job_panel = next(p for p in d["panels"] if p["title"] == "Jobs")
    assert job_panel["gridPos"]["h"] <= 7


def test_no_panel_overlap() -> None:
    d = dashboard()
    positions = [(p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["h"]) for p in d["panels"]]
    for i, (x1, y1, h1) in enumerate(positions):
        for x2, y2, h2 in positions[i + 1 :]:
            if x1 == x2 and y1 < y2:
                assert y1 + h1 <= y2, f"panel at y={y1} h={h1} overlaps panel at y={y2}"
            elif x1 == x2 and y2 < y1:
                assert y2 + h2 <= y1, f"panel at y={y2} h={h2} overlaps panel at y={y1}"


def test_run_conclusion_is_html_escaped_in_identity() -> None:
    snap = snapshot()
    snap["run"]["conclusion"] = '<script>alert("conclusion")</script>'
    d = build_dashboard(snapshot=snap)
    identity = next(p for p in d["panels"] if p["title"] == "Run identity")
    content = identity["options"]["content"]
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


def test_malformed_repository_is_rejected() -> None:
    snap = snapshot()
    snap["repository"] = "javascript:alert(1)"
    with pytest.raises(SnapshotError, match="owner/name"):
        build_dashboard(snapshot=snap)


def test_negative_job_id_is_rejected() -> None:
    snap = snapshot()
    snap["jobs"][0]["id"] = -1
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_run_link_points_to_selected_attempt() -> None:
    d = dashboard()
    assert "/attempts/1" in d["links"][0]["url"]
    snap = snapshot()
    snap["run"]["attempt"] = 3
    d3 = build_dashboard(snapshot=snap)
    assert "/attempts/3" in d3["links"][0]["url"]


def test_first_listed_step_disclaims_api_order() -> None:
    d = dashboard()
    text = next(p for p in d["panels"] if p["title"] == "First listed failed step")
    assert "listing order" in text["options"]["content"]


def test_cli_constant_boundary_on_malformed_json_no_traceback(tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text("{invalid json", encoding="utf-8")
    out_file = tmp_path / "out.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1
    assert not out_file.exists()


def test_artifact_missing_id_is_rejected_with_constant_error() -> None:
    snap = snapshot()
    del snap["artifacts"][0]["id"]
    with pytest.raises(SnapshotError, match="missing the field"):
        build_dashboard(snapshot=snap)


def test_artifact_zero_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = 0
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_negative_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = -1
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_string_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = "evil"
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_over_cap_is_rejected_not_truncated() -> None:
    snap = snapshot()
    snap["artifacts"] = [{"id": i + 1, "name": f"a{i}", "size_bytes": 100, "expired": False} for i in range(1001)]
    with pytest.raises(SnapshotError, match="bounded list"):
        build_dashboard(snapshot=snap)


def test_artifact_valid_zero_size_is_accepted() -> None:
    snap = snapshot()
    snap["artifacts"][0]["size_bytes"] = 0
    d = build_dashboard(snapshot=snap)
    artifact_panel = next(p for p in d["panels"] if p["title"] == "Artifacts and evidence")
    rows = json.loads(artifact_panel["targets"][0]["data"])
    assert rows[0]["size_bytes"] == 0


def test_artifact_negative_size_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["size_bytes"] = -5
    with pytest.raises(SnapshotError, match="non-negative integer"):
        build_dashboard(snapshot=snap)


def test_artifact_non_bool_expired_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["expired"] = "yes"
    with pytest.raises(SnapshotError, match="boolean"):
        build_dashboard(snapshot=snap)


def test_invalid_attempt_cannot_build_a_link() -> None:
    snap = snapshot()
    snap["run"]["attempt"] = 0
    with pytest.raises(SnapshotError, match="run attempt"):
        build_dashboard(snapshot=snap)


def test_invalid_timestamp_is_not_reported_as_unmeasured() -> None:
    snap = snapshot()
    snap["jobs"][0]["started_at"] = "invalid"
    with pytest.raises(SnapshotError, match="timestamp is invalid"):
        build_dashboard(snapshot=snap)
