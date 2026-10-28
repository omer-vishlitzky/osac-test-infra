"""Generate a read-only Grafana run-investigation dashboard from one
GitHub snapshot.

The snapshot is a small, explicit JSON contract shared with the pilot
collector. Everything the dashboard shows is embedded in the dashboard's
own Infinity inline-data queries: there is no live collection, no API
server, and no credentials. Refresh is manual, through this CLI.

Usage:
    python -m monitoring.ci_observability.pilot_dashboard \
        --snapshot /private/snapshot.json --output /private/dashboard.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime
from html import escape as _escape_html
from pathlib import Path
from typing import TypeAlias

JSONValue: TypeAlias = "bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None"
JSONObject: TypeAlias = "dict[str, JSONValue]"

DASHBOARD_UID = "ci-run-pilot"
DATASOURCE_UID = "ci-pilot-inline"
INFINITY_TYPE = "yesoreyeram-infinity-datasource"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_JOBS = 500
MAX_STEPS_PER_JOB = 200
MAX_ARTIFACTS = 1000

REQUIRED_SNAPSHOT_KEYS = ("schema", "version", "fetched_at", "repository", "run", "jobs", "coverage")
REQUIRED_RUN_KEYS = ("id", "attempt", "name", "event", "status", "conclusion", "head_sha", "created_at", "updated_at")
REQUIRED_JOB_KEYS = ("id", "name", "status", "conclusion", "started_at", "completed_at", "url", "steps")
REQUIRED_STEP_KEYS = ("number", "name", "status", "conclusion")


class SnapshotError(ValueError):
    """Constant-safe: the message names the violation, never the value."""


def _load_snapshot(*, path: Path) -> JSONObject:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError("snapshot must be a regular file")
        if metadata.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot exceeds the size bound")
        raw = source.read(MAX_SNAPSHOT_BYTES + 1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds the size bound")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SnapshotError("snapshot must be strict UTF-8") from None
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise SnapshotError("snapshot is not valid JSON") from None
    if not isinstance(document, dict):
        raise SnapshotError("snapshot must be a JSON object")
    return document


def _validate_snapshot(*, snapshot: JSONObject) -> None:
    for key in REQUIRED_SNAPSHOT_KEYS:
        if key not in snapshot:
            raise SnapshotError(f"snapshot is missing the field: {key}")
    if snapshot["schema"] != "osac.ci-observability.pilot":
        raise SnapshotError("snapshot schema is not recognized")
    if snapshot["version"] != 1:
        raise SnapshotError("snapshot version is not supported")
    run = snapshot["run"]
    if not isinstance(run, dict):
        raise SnapshotError("snapshot run must be an object")
    for key in REQUIRED_RUN_KEYS:
        if key not in run:
            raise SnapshotError(f"snapshot run is missing the field: {key}")
    jobs = snapshot["jobs"]
    if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
        raise SnapshotError("snapshot jobs must be a bounded list")
    for job in jobs:
        if not isinstance(job, dict):
            raise SnapshotError("snapshot job must be an object")
        for key in REQUIRED_JOB_KEYS:
            if key not in job:
                raise SnapshotError(f"snapshot job is missing the field: {key}")
        steps = job["steps"]
        if not isinstance(steps, list) or len(steps) > MAX_STEPS_PER_JOB:
            raise SnapshotError("snapshot job steps must be a bounded list")
        for step in steps:
            if not isinstance(step, dict):
                raise SnapshotError("snapshot step must be an object")
            for key in REQUIRED_STEP_KEYS:
                if key not in step:
                    raise SnapshotError(f"snapshot step is missing the field: {key}")
    coverage = snapshot["coverage"]
    if not isinstance(coverage, dict):
        raise SnapshotError("snapshot coverage must be an object")
    artifacts = snapshot.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
            raise SnapshotError("snapshot artifacts must be a bounded list")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise SnapshotError("snapshot artifact must be an object")
            for key in ("id", "name", "size_bytes", "expired"):
                if key not in artifact:
                    raise SnapshotError(f"snapshot artifact is missing the field: {key}")
            if not isinstance(artifact["id"], int) or isinstance(artifact["id"], bool) or artifact["id"] < 1:
                raise SnapshotError("artifact id must be a positive integer")
            if not isinstance(artifact["name"], str):
                raise SnapshotError("artifact name must be a string")
            size = artifact["size_bytes"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise SnapshotError("artifact size_bytes must be a non-negative integer")
            if not isinstance(artifact["expired"], bool):
                raise SnapshotError("artifact expired must be a boolean")


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validated_repository(*, repository: object) -> str:
    if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
        raise SnapshotError("repository is not in owner/name form")
    return repository


def _validated_positive_int(*, value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SnapshotError(f"{name} must be a positive integer")
    return value


def _github_url(*, repository: str, kind: str, ident: int) -> str:
    return f"https://github.com/{repository}/{kind}/{ident}"


def _run_url(*, repository: str, run_id: int, attempt: int) -> str:
    return f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"


def _duration_seconds(*, started: str | None, ended: str | None) -> float | None:
    """Duration from two ISO strings, or None when either bound is absent."""
    if started is None or ended is None:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise SnapshotError("snapshot timestamp is invalid") from None
    return round((end - start).total_seconds(), 1)


def _safe_link(*, url: str, text: str) -> str:
    if not url.startswith(("http://", "https://")) or url.startswith("javascript:"):
        return _escape_html(text)
    return f'<a href="{_escape_html(url)}" target="_blank" rel="noopener noreferrer">{_escape_html(text)}</a>'


def _inline_target(*, ref_id: str, data: list[JSONObject], columns: list[dict[str, str]]) -> JSONObject:
    return {
        "refId": ref_id,
        "datasource": {"type": INFINITY_TYPE, "uid": DATASOURCE_UID},
        "type": "json",
        "source": "inline",
        "format": "table",
        "parser": "backend",
        "data": json.dumps(data),
        "columns": columns,
        "root_selector": "",
        "url": "",
    }


def _text_panel(*, panel_id: int, title: str, description: str, content: str, grid_pos: dict[str, int]) -> JSONObject:
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "text",
        "gridPos": grid_pos,
        "options": {"mode": "html", "content": content},
    }


def build_dashboard(*, snapshot: JSONObject) -> JSONObject:
    """Build the complete Grafana dashboard JSON from one validated snapshot."""

    _validate_snapshot(snapshot=snapshot)
    repository = _validated_repository(repository=snapshot["repository"])
    run = snapshot["run"]
    run_id = _validated_positive_int(value=run["id"], name="run id")
    attempt = _validated_positive_int(value=run["attempt"], name="run attempt")
    jobs = snapshot["jobs"]
    for job in jobs:
        _validated_positive_int(value=job["id"], name="job id")
    coverage = snapshot["coverage"]

    run_lines = [
        f"<h3>{_escape_html(run['name'])}</h3>",
        "<table>",
        f"<tr><td><b>Repository</b></td><td>{_escape_html(repository)}</td></tr>",
        f"<tr><td><b>Run</b></td><td>#{run_id} (attempt {attempt})</td></tr>",
        f"<tr><td><b>Event</b></td><td>{_escape_html(run['event'])}</td></tr>",
        f"<tr><td><b>Status</b></td><td>{_escape_html(run['status'])}"
        + (f" / {_escape_html(run['conclusion'])}" if run.get("conclusion") else " / unknown")
        + "</td></tr>",
        f"<tr><td><b>GitHub head SHA</b></td><td><code>{_escape_html(run['head_sha'])}</code></td></tr>",
        "<tr><td><b>Tested revision</b></td><td><b>UNVERIFIED</b></td></tr>",
        f"<tr><td><b>Created</b></td><td>{_escape_html(run['created_at'])}</td></tr>",
        f"<tr><td><b>Updated</b></td><td>{_escape_html(run['updated_at'])}</td></tr>",
        f"<tr><td><b>Snapshot taken</b></td><td>{_escape_html(snapshot['fetched_at'])}</td></tr>",
        "</table>",
        "<p>"
        + _safe_link(
            url=_run_url(repository=repository, run_id=run_id, attempt=attempt), text="Open this run on GitHub"
        )
        + "</p>",
    ]
    if attempt > 1:
        run_lines.append(
            "<p>Note: this is attempt "
            + str(attempt)
            + ". Previous attempts have separate GitHub results; this dashboard does not claim they are comparable.</p>"
        )
    if run.get("workflow_path"):
        run_lines.append(f"<p>Workflow: <code>{_escape_html(run['workflow_path'])}</code></p>")

    job_rows = []
    for job in jobs:
        duration = _duration_seconds(started=job.get("started_at"), ended=job.get("completed_at"))
        job_rows.append(
            {
                "job": job["name"],
                "status": job["status"],
                "conclusion": job["conclusion"] or "unknown",
                "duration_s": duration,
                "url": _github_url(repository=repository, kind="actions/runs", ident=run_id) + f"/job/{job['id']}",
                "started": job.get("started_at") or "",
                "completed": job.get("completed_at") or "",
            }
        )
    job_columns = [
        {"selector": "job", "text": "Job", "type": "string"},
        {"selector": "status", "text": "Status", "type": "string"},
        {"selector": "conclusion", "text": "Conclusion", "type": "string"},
        {"selector": "duration_s", "text": "Duration (s)", "type": "number"},
        {"selector": "url", "text": "GitHub", "type": "string"},
        {"selector": "started", "text": "Started", "type": "string"},
        {"selector": "completed", "text": "Completed", "type": "string"},
    ]

    step_rows = []
    for job in jobs:
        for step in job.get("steps", []):
            duration = _duration_seconds(started=step.get("started_at"), ended=step.get("completed_at"))
            step_rows.append(
                {
                    "job": job["name"],
                    "step": step["name"],
                    "number": step["number"],
                    "status": step["status"],
                    "conclusion": step["conclusion"] or "unknown",
                    "duration_s": duration,
                    "started": step.get("started_at") or "",
                    "completed": step.get("completed_at") or "",
                }
            )
    step_columns = [
        {"selector": "job", "text": "Job", "type": "string"},
        {"selector": "step", "text": "Step", "type": "string"},
        {"selector": "number", "text": "#", "type": "number"},
        {"selector": "status", "text": "Status", "type": "string"},
        {"selector": "conclusion", "text": "Conclusion", "type": "string"},
        {"selector": "duration_s", "text": "Duration (s)", "type": "number"},
        {"selector": "started", "text": "Started", "type": "string"},
        {"selector": "completed", "text": "Completed", "type": "string"},
    ]

    failed_steps = [s for s in step_rows if s["conclusion"] in ("failure", "cancelled", "timed_out")]
    first_failed_content = "<p>No failed, cancelled, or timed-out step observed in this snapshot.</p>"
    if failed_steps:
        first = failed_steps[0]
        first_failed_content = (
            f"<p><b>First listed failed step:</b> {_escape_html(first['step'])} "
            f"in job {_escape_html(first['job'])} "
            f"(conclusion: {_escape_html(first['conclusion'])}).</p>"
            "<p>This is the first failed step in the GitHub API's listing order, "
            "which does not prove it is the earliest cause. The failing step may be a downstream effect.</p>"
        )

    artifact_rows = []
    for artifact in snapshot.get("artifacts", []):
        artifact_id = _validated_positive_int(value=artifact["id"], name="artifact id")
        artifact_rows.append(
            {
                "name": artifact["name"],
                "size_bytes": artifact["size_bytes"],
                "expired": "yes" if artifact["expired"] else "no",
                "url": _github_url(repository=repository, kind="actions/runs", ident=run_id)
                + f"/artifacts/{artifact_id}",
            }
        )
    artifact_columns = [
        {"selector": "name", "text": "Artifact", "type": "string"},
        {"selector": "size_bytes", "text": "Size (bytes)", "type": "number"},
        {"selector": "expired", "text": "Expired", "type": "string"},
        {"selector": "url", "text": "Link", "type": "string"},
    ]

    coverage_notes = coverage.get("notes", [])
    coverage_content = (
        "<h4>Source coverage</h4><ul>"
        f"<li>Jobs complete: {'yes' if coverage.get('jobs_complete') else 'NO'}</li>"
        f"<li>Artifacts complete: {'yes' if coverage.get('artifacts_complete') else 'NO'}</li>"
        f"<li>Test results: {_escape_html(str(coverage.get('test_results', 'unknown')))}</li>"
        f"<li>Tested revision: {_escape_html(str(coverage.get('tested_revision', 'unknown')))}</li>"
        "</ul>"
    )
    if coverage_notes:
        coverage_content += "<p><b>Notes:</b></p><ul>"
        for note in coverage_notes[:10]:
            coverage_content += f"<li>{_escape_html(str(note))}</li>"
        coverage_content += "</ul>"
    coverage_content += (
        "<p>This dashboard is a standalone snapshot. "
        "It does not claim live collection, test results, or cluster state.</p>"
    )

    panels: list[JSONObject] = []
    panel_id = 1

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="Run identity",
            description="Who, what, when, and the explicit UNVERIFIED tested-revision marker.",
            content="\n".join(run_lines),
            grid_pos={"h": 8, "w": 8, "x": 0, "y": 0},
        )
    )
    panel_id += 1

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="Source coverage and limitations",
            description="What this snapshot does and does not cover.",
            content=coverage_content,
            grid_pos={"h": 8, "w": 8, "x": 8, "y": 0},
        )
    )
    panel_id += 1

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="First listed failed step",
            description="The first step with a failure conclusion. Not a root-cause claim.",
            content=first_failed_content,
            grid_pos={"h": 8, "w": 8, "x": 16, "y": 0},
        )
    )
    panel_id += 1

    job_link_overrides = [
        {
            "matcher": {"id": "byName", "options": "GitHub"},
            "properties": [
                {"id": "links", "value": [{"title": "Open on GitHub", "url": "${__value.raw}", "targetBlank": True}]},
                {"id": "custom.cellOptions", "value": {"type": "data-links"}},
                {"id": "displayName", "value": "GitHub"},
                {"id": "custom.width", "value": 120},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "Duration (s)"},
            "properties": [
                {"id": "displayName", "value": "Duration (s)"},
                {"id": "custom.width", "value": 120},
                {"id": "unit", "value": "s"},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "Job"},
            "properties": [{"id": "displayName", "value": "Job"}, {"id": "custom.width", "value": 300}],
        },
    ]
    panels.append(
        {
            "id": panel_id,
            "title": "Jobs",
            "description": f"{len(jobs)} job(s). Durations use actual start and end; open jobs have no duration.",
            "type": "table",
            "gridPos": {"h": 7, "w": 24, "x": 0, "y": 8},
            "targets": [_inline_target(ref_id="A", data=job_rows, columns=job_columns)],
            "transformations": [
                {
                    "id": "organize",
                    "options": {
                        "indexByName": {
                            "Job": 0,
                            "Conclusion": 1,
                            "Duration (s)": 2,
                            "Status": 3,
                            "GitHub": 4,
                            "Started": 5,
                            "Completed": 6,
                        },
                        "excludeByName": {},
                        "renameByName": {},
                    },
                }
            ],
            "fieldConfig": {"defaults": {}, "overrides": job_link_overrides},
            "options": {
                "showHeader": True,
                "cellHeight": "sm",
                "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
            },
        }
    )
    panel_id += 1

    step_overrides = [
        {
            "matcher": {"id": "byName", "options": "Step"},
            "properties": [{"id": "displayName", "value": "Step"}, {"id": "custom.width", "value": 360}],
        },
        {
            "matcher": {"id": "byName", "options": "Duration (s)"},
            "properties": [
                {"id": "displayName", "value": "Duration (s)"},
                {"id": "custom.width", "value": 120},
                {"id": "unit", "value": "s"},
            ],
        },
        {
            "matcher": {"id": "byName", "options": "Job"},
            "properties": [{"id": "displayName", "value": "Job"}, {"id": "custom.width", "value": 240}],
        },
        {"matcher": {"id": "byName", "options": "#"}, "properties": [{"id": "custom.width", "value": 50}]},
        {"matcher": {"id": "byName", "options": "Started"}, "properties": [{"id": "custom.width", "value": 200}]},
        {"matcher": {"id": "byName", "options": "Completed"}, "properties": [{"id": "custom.width", "value": 200}]},
    ]
    panels.append(
        {
            "id": panel_id,
            "title": "Steps (all jobs)",
            "description": f"{len(step_rows)} step(s) across {len(jobs)} job(s), flattened.",
            "type": "table",
            "gridPos": {"h": 12, "w": 24, "x": 0, "y": 15},
            "targets": [_inline_target(ref_id="A", data=step_rows, columns=step_columns)],
            "transformations": [
                {
                    "id": "organize",
                    "options": {
                        "indexByName": {
                            "Step": 0,
                            "Conclusion": 1,
                            "Duration (s)": 2,
                            "Job": 3,
                            "Started": 4,
                            "Completed": 5,
                            "#": 6,
                            "Status": 7,
                        },
                        "excludeByName": {},
                        "renameByName": {},
                    },
                }
            ],
            "fieldConfig": {"defaults": {}, "overrides": step_overrides},
            "options": {
                "showHeader": True,
                "cellHeight": "sm",
                "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
            },
        }
    )
    panel_id += 1

    if artifact_rows:
        artifact_link_overrides = [
            {
                "matcher": {"id": "byName", "options": "Link"},
                "properties": [
                    {
                        "id": "links",
                        "value": [{"title": "Open on GitHub", "url": "${__value.raw}", "targetBlank": True}],
                    },
                    {"id": "custom.cellOptions", "value": {"type": "data-links"}},
                ],
            }
        ]
        panels.append(
            {
                "id": panel_id,
                "title": "Artifacts and evidence",
                "description": f"{len(artifact_rows)} artifact(s). Expired artifacts cannot be downloaded.",
                "type": "table",
                "gridPos": {"h": 8, "w": 24, "x": 0, "y": 27},
                "targets": [_inline_target(ref_id="A", data=artifact_rows, columns=artifact_columns)],
                "transformations": [
                    {
                        "id": "organize",
                        "options": {
                            "indexByName": {"Artifact": 0, "Expired": 1, "Size (bytes)": 2, "Link": 3},
                            "excludeByName": {},
                            "renameByName": {},
                        },
                    }
                ],
                "fieldConfig": {"defaults": {}, "overrides": artifact_link_overrides},
                "options": {
                    "showHeader": True,
                    "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
                },
            }
        )
        panel_id += 1

    dashboard: JSONObject = {
        "uid": DASHBOARD_UID,
        "title": f"CI Run: {_escape_html(run['name'])} #{run_id} attempt {attempt}",
        "description": (
            f"Read-only investigation snapshot for run #{run_id} (attempt {attempt}) "
            f"of {_escape_html(repository)}. Data is embedded; refresh is manual. "
            "Tested revision is UNVERIFIED."
        ),
        "tags": ["ci-pilot"],
        "editable": False,
        "graphTooltip": 0,
        "schemaVersion": 39,
        "version": 1,
        "refresh": False,
        "timezone": "utc",
        "time": {"from": run["created_at"], "to": run["updated_at"]},
        "timepicker": {"refresh_intervals": []},
        "templating": {"list": []},
        "annotations": {"list": []},
        "links": [
            {
                "title": "GitHub run",
                "type": "link",
                "url": _run_url(repository=repository, run_id=run_id, attempt=attempt),
                "targetBlank": True,
            }
        ],
        "panels": panels,
    }
    return dashboard


def _write_output(*, dashboard: JSONObject, path: Path) -> None:
    if path.is_symlink():
        raise SnapshotError("output must not be a symlink")
    if path.exists():
        raise SnapshotError("output already exists; refusing to overwrite")
    parent = path.parent
    if not parent.is_dir():
        raise SnapshotError("output parent directory does not exist")
    payload = json.dumps(dashboard, indent=2, sort_keys=False) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        data = payload.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m monitoring.ci_observability.pilot_dashboard",
        description="Generate a read-only Grafana run-investigation dashboard from one GitHub snapshot.",
    )
    parser.add_argument("--snapshot", required=True, help="Path to the snapshot JSON file")
    parser.add_argument("--output", required=True, help="Path for the output dashboard JSON")
    args = parser.parse_args(argv)

    snapshot_path = Path(args.snapshot)
    output_path = Path(args.output)

    try:
        snapshot = _load_snapshot(path=snapshot_path)
        dashboard = build_dashboard(snapshot=snapshot)
        _write_output(dashboard=dashboard, path=output_path)
    except SnapshotError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError) as error:
        print(f"ERROR: snapshot cannot be processed: {type(error).__name__}", file=sys.stderr)
        return 1

    print(f"Dashboard written: {output_path} (mode 600, {len(dashboard['panels'])} panels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
