"""Read-only HTTP API for the CI observability ledger (W01a).

Serves bounded ledger queries on a loopback port (default 9185) for
Grafana and local diagnostics: GET only, no write endpoint, no outbound
request, parameterized SQL. Invalid input is a 400 with a fixed message
that echoes no request value, server-side failures are an opaque 500, and
diagnostics log exception types only because exception text can carry SQL
and connection strings. Accepted connections and concurrent requests are
both capped, and every socket and database connection carries a finite
timeout.

Run from the repository root with the DSN in CI_OBSERVABILITY_DSN, never
in argv (argv is readable through /proc and can carry a password):

    CI_OBSERVABILITY_DSN=postgresql://ci_observability_reader@host/db \\
        python -m monitoring.ci_observability.api serve

Migrations use the ingest role; the API uses the SELECT-only reader role.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Sequence
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import ledger
from .ledger import (
    MAX_LIST_ROWS,
    MAX_OFFSET,
    ExecutionRow,
    JSONValue,
    Ledger,
    LedgerError,
    RunSummary,
    StoredEvent,
    ValidationError,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9185
MAX_REQUEST_PATH_BYTES = 2048
MAX_QUERY_FIELDS = 16


class _BadRequest(Exception):
    """A request the API refuses before running any query. The message is fixed text."""


def _require_loopback(*, host: str) -> None:
    if host == "localhost":
        return
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("read API host must be a loopback address") from None
    if not parsed.is_loopback:
        raise ValueError("read API host must be a loopback address")


class ReadAPIServer(ThreadingHTTPServer):
    """Read-only HTTP server on a loopback port with bounded connections.

    max_connections caps accepted connections before a handler thread is
    spawned; socket_timeout_seconds is the finite header, body, and write
    timeout that expires idle connections; max_concurrent_requests also
    bounds the database connections, one per request.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = ledger.DEFAULT_SCHEMA,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        max_concurrent_requests: int = 8,
        request_wait_seconds: float = 10.0,
        max_connections: int = 16,
        socket_timeout_seconds: float = 15.0,
    ) -> None:
        _require_loopback(host=host)
        if max_concurrent_requests < 1 or request_wait_seconds <= 0:
            raise ValueError("max_concurrent_requests must be >= 1 and request_wait_seconds positive")
        if max_connections < 1 or socket_timeout_seconds <= 0:
            raise ValueError("max_connections must be >= 1 and socket_timeout_seconds positive")
        self.dsn = dsn
        self.schema = schema
        self.request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.request_wait_seconds = request_wait_seconds
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        self.socket_timeout_seconds = socket_timeout_seconds
        super().__init__((host, port), _ReadAPIHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self.connection_slots.acquire(blocking=False):
            # Overloaded: refuse before any thread or database work exists.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        # Type only: the socketserver default prints a full traceback with request data.
        exc = sys.exc_info()[1]
        print(f"ci-observability-api: connection failed with {type(exc).__name__}", file=sys.stderr)


class _ReadAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        # One finite timeout for header reads, body reads, and writes; an
        # idle keepalive connection expires and releases its slot.
        self.connection.settimeout(self.server.socket_timeout_seconds)

    def do_GET(self) -> None:
        if len(self.path.encode("utf-8")) > MAX_REQUEST_PATH_BYTES:
            self._send_json(status=400, body={"error": "request path too long"})
            return
        if not self.server.request_slots.acquire(timeout=self.server.request_wait_seconds):
            self._send_json(status=503, body={"error": "server busy"})
            return
        try:
            try:
                status, body = self._route()
            except _BadRequest as exc:
                status, body = 400, {"error": str(exc)}
            except Exception as exc:
                # Exception types only: message text can carry SQL and
                # connection strings, and the client gets an opaque body.
                print(f"ci-observability-api: request failed with {type(exc).__name__}", file=sys.stderr)
                status, body = 500, {"error": "internal error"}
        finally:
            self.server.request_slots.release()
        self._send_json(status=status, body=body)

    def do_POST(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        if code == 501:
            self._reject_method()
            return
        self._send_json(status=code, body={"error": "invalid HTTP request"})

    def log_message(self, format: str, *args: object) -> None:
        # Suppress the default request log: request paths carry untrusted
        # input and must not be written to logs unescaped.
        del format, args

    def _reject_method(self) -> None:
        # Rejected requests close: no keepalive for refused methods.
        self.close_connection = True
        self._send_json(status=405, body={"error": "method not allowed", "allow": "GET"})

    def _send_json(self, *, status: int, body: dict[str, JSONValue]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if status == 405:
            self.send_header("Allow", "GET")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _ledger(self) -> Ledger:
        return Ledger(
            dsn=self.server.dsn,
            schema=self.server.schema,
            application_name="ci-observability-read-api",
            connect_timeout_seconds=5.0,
            statement_timeout_ms=5000,
            lock_timeout_ms=2000,
        )

    def _route(self) -> tuple[int, dict[str, JSONValue]]:
        parsed = urlparse(self.path)
        params = self._parse_query(raw=parsed.query)
        if len(params) > MAX_QUERY_FIELDS:
            raise _BadRequest("too many query parameters")
        if parsed.path == "/healthz":
            return self._health(params=params)
        if parsed.path == "/api/v1/runs":
            return self._runs(params=params)
        if parsed.path == "/api/v1/executions":
            return self._executions(params=params)
        if parsed.path.startswith("/api/v1/executions/"):
            remainder = parsed.path[len("/api/v1/executions/") :]
            if remainder.endswith("/events"):
                execution_id = unquote(remainder[: -len("/events")])
                return self._events(execution_id=execution_id, params=params)
            if remainder and "/" not in remainder:
                return self._execution(execution_id=unquote(remainder), params=params)
        return 404, {"error": "not found"}

    def _parse_query(self, *, raw: str) -> dict[str, str]:
        if not raw:
            return {}
        try:
            parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise _BadRequest("malformed query string") from exc
        params: dict[str, str] = {}
        for key, values in parsed.items():
            if len(values) != 1:
                raise _BadRequest("a query parameter must appear exactly once")
            params[key] = values[0]
        return params

    def _health(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            client.health_check()
        except Exception as exc:
            print(f"ci-observability-api: health check failed with {type(exc).__name__}", file=sys.stderr)
            return 503, {"status": "unavailable"}
        finally:
            client.close()
        return 200, {"status": "ok"}

    def _runs(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        repository_id = _query_int(params=params, name="repository_id")
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            runs = client.list_runs(repository_id=repository_id, limit=limit, offset=offset)
        finally:
            client.close()
        return 200, {"runs": [_run_json(run=run) for run in runs], "count": len(runs), "limit": limit, "offset": offset}

    def _executions(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        repository_id = _query_int(params=params, name="repository_id")
        run_id = _query_int(params=params, name="run_id")
        run_attempt = _query_int(params=params, name="run_attempt")
        github_job_id = _query_int(params=params, name="github_job_id")
        logical_job_key = _query_pattern(params=params, name="logical_job_key", pattern=ledger.LOGICAL_JOB_KEY_PATTERN)
        executed = _query_bool(params=params, name="executed")
        registered_after = _query_timestamp(params=params, name="registered_after")
        registered_before = _query_timestamp(params=params, name="registered_before")
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            rows = client.list_executions(
                repository_id=repository_id,
                run_id=run_id,
                run_attempt=run_attempt,
                logical_job_key=logical_job_key,
                github_job_id=github_job_id,
                executed=executed,
                registered_after=registered_after,
                registered_before=registered_before,
                limit=limit,
                offset=offset,
            )
        finally:
            client.close()
        return 200, {
            "executions": [_execution_json(row=row) for row in rows],
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }

    def _execution(self, *, execution_id: str, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        _reject_leftovers(params=params)
        _validate_execution_id(execution_id=execution_id)
        client = self._ledger()
        try:
            row = client.get_execution(execution_id=execution_id)
            if row is None:
                return 404, {"error": "execution not found"}
            event_count, journal_count = client.execution_counts(execution_id=execution_id)
        finally:
            client.close()
        body = _execution_json(row=row)
        body["event_count"] = event_count
        body["journal_count"] = journal_count
        return 200, body

    def _events(self, *, execution_id: str, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        journal_id = _query_pattern(params=params, name="journal_id", pattern=ledger.JOURNAL_ID_PATTERN)
        recorded_after = _query_timestamp(params=params, name="recorded_after")
        recorded_before = _query_timestamp(params=params, name="recorded_before")
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        _validate_execution_id(execution_id=execution_id)
        client = self._ledger()
        try:
            if client.get_execution(execution_id=execution_id) is None:
                return 404, {"error": "execution not found"}
            events = client.list_events(
                execution_id=execution_id,
                journal_id=journal_id,
                recorded_after=recorded_after,
                recorded_before=recorded_before,
                limit=limit,
                offset=offset,
            )
        finally:
            client.close()
        return 200, {
            "execution_id": execution_id,
            "events": [_event_json(event=event) for event in events],
            "count": len(events),
            "limit": limit,
            "offset": offset,
        }


def _validate_execution_id(*, execution_id: str) -> None:
    if not ledger.EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise _BadRequest("execution id must be a bounded filename-safe identifier")


def _reject_leftovers(*, params: dict[str, str]) -> None:
    if params:
        raise _BadRequest("unknown query parameter")


def _query_int(*, params: dict[str, str], name: str) -> int | None:
    if name not in params:
        return None
    raw = params.pop(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"{name} must be an integer >= 1") from exc
    if value < 1:
        raise _BadRequest(f"{name} must be an integer >= 1")
    return value


def _query_limit(*, params: dict[str, str]) -> int:
    if "limit" not in params:
        return ledger.DEFAULT_LIST_ROWS
    raw = params.pop("limit")
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"limit must be an integer between 1 and {MAX_LIST_ROWS}") from exc
    if not 1 <= value <= MAX_LIST_ROWS:
        raise _BadRequest(f"limit must be an integer between 1 and {MAX_LIST_ROWS}")
    return value


def _query_offset(*, params: dict[str, str]) -> int:
    if "offset" not in params:
        return 0
    raw = params.pop("offset")
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"offset must be an integer between 0 and {MAX_OFFSET}") from exc
    if not 0 <= value <= MAX_OFFSET:
        raise _BadRequest(f"offset must be an integer between 0 and {MAX_OFFSET}")
    return value


def _query_bool(*, params: dict[str, str], name: str) -> bool | None:
    if name not in params:
        return None
    raw = params.pop(name)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise _BadRequest(f"{name} must be true or false")


def _query_pattern(*, params: dict[str, str], name: str, pattern: re.Pattern[str]) -> str | None:
    if name not in params:
        return None
    raw = params.pop(name)
    if not pattern.fullmatch(raw):
        raise _BadRequest(f"{name} must be a bounded identifier")
    return raw


def _query_timestamp(*, params: dict[str, str], name: str) -> datetime | None:
    if name not in params:
        return None
    raw = params.pop(name)
    try:
        return ledger.parse_utc_text(value=raw)
    except ValidationError as exc:
        raise _BadRequest(f"{name} must be a UTC ISO-8601 timestamp ending in Z") from exc


def _run_json(*, run: RunSummary) -> dict[str, JSONValue]:
    return {
        "repository_id": run.repository_id,
        "run_id": run.run_id,
        "run_attempts": list(run.run_attempts),
        "execution_count": run.execution_count,
        "registration_count": run.registration_count,
    }


def _execution_json(*, row: ExecutionRow) -> dict[str, JSONValue]:
    return {
        "execution_id": row.execution_id,
        "schema_version": row.schema_version,
        "repository_id": row.repository_id,
        "run_id": row.run_id,
        "run_attempt": row.run_attempt,
        "logical_job_key": row.logical_job_key,
        "github_job_id": row.github_job_id,
        "executed": row.executed,
        "github_run_conclusion": row.github_run_conclusion,
        "github_job_conclusion": row.github_job_conclusion,
        "registered_at": ledger.format_utc(value=row.registered_at),
    }


def _event_json(*, event: StoredEvent) -> dict[str, JSONValue]:
    return {
        "execution_id": event.execution_id,
        "journal_id": event.journal_id,
        "sequence": event.sequence,
        "schema_version": event.schema_version,
        # The original delivered timestamp text, not a re-rendered form.
        "recorded_at": event.recorded_at,
        "source_at": event.source_at,
        "mono_ns": event.mono_ns,
        "invocation_id": event.invocation_id,
        "clock_id": event.clock_id,
        "clock_uncertainty_ns": event.clock_uncertainty_ns,
        "event": event.event,
        "data": event.data,
        "ingested_at": ledger.format_utc(value=event.ingested_at),
    }


def serve(*, dsn: str, schema: str = ledger.DEFAULT_SCHEMA, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ReadAPIServer(dsn=dsn, schema=schema, host=host, port=port)
    print(f"ci-observability read API listening on http://{host}:{server.port} (schema {schema})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _CLIArgumentParser(argparse.ArgumentParser):
    """Never echoes argv back: the default error output repeats the offending
    argument, which can be a DSN passed by habit."""

    def error(self, message: str) -> None:
        print("ci-observability-api: invalid arguments", file=sys.stderr)
        raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _CLIArgumentParser(
        prog="ci-observability-api",
        description="Migrate the CI observability ledger schema or serve its read-only HTTP API.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_command = commands.add_parser("migrate", help="apply pending ledger migrations")
    migrate_command.add_argument("--schema", default=ledger.DEFAULT_SCHEMA, help="target schema (default: %(default)s)")
    serve_command = commands.add_parser("serve", help="serve the read-only HTTP API on the loopback interface")
    serve_command.add_argument("--schema", default=ledger.DEFAULT_SCHEMA, help="schema to read (default: %(default)s)")
    serve_command.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port (default: %(default)s)")
    args = parser.parse_args(argv)

    # The DSN can carry a password, and argv is readable through /proc, so
    # it is read from the environment and never accepted as an argument.
    dsn = os.environ.get("CI_OBSERVABILITY_DSN", "").strip()
    if not dsn:
        print("CI_OBSERVABILITY_DSN is not set: the DSN is required and is read from the environment", file=sys.stderr)
        return 2
    try:
        if args.command == "migrate":
            applied = ledger.apply_migrations(dsn=dsn, schema=args.schema)
            if applied:
                print(f"applied migrations: {', '.join(str(version) for version in applied)}")
            else:
                print("schema is up to date")
        else:
            serve(dsn=dsn, schema=args.schema, port=args.port)
    except LedgerError as exc:
        # Ledger errors are curated, value-free text.
        print(f"ci-observability-api: {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Everything else is reported by type only: exception text can
        # echo the DSN or SQL.
        print(f"ci-observability-api: {args.command} failed with {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
